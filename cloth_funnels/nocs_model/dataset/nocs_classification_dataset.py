from typing import List, Optional
import os
import copy

import h5py
import numpy as np
import torch
from torch.utils.data import Dataset
from torch.utils.data.dataloader import DataLoader
import pytorch_lightning as pl
# import imgaug.augmenters as iaa
from imgaug.augmentables.batches import Batch
from collections import defaultdict
from itertools import chain
from tqdm import tqdm
import cv2
import torchvision

class NOCSDataset(Dataset):
    def __init__(self,
            # data
            hdf5_path: str,
            sample_keys: Optional[List[str]]=None,
            # data params,
            nocs_dims: List[int]=[0,2],
            bins: int=64,
            ignore_index: int=-1,
            include_coverage: bool=False,
            # augumentation
            aug_enable: bool=True,
            aug_prob: float=0.5,
            rot_range: float=np.pi/4,
            shift_range: float=0.2,
            # training params
            static_epoch_seed: bool=False,
            **kwargs):
        super().__init__()
        self.file = h5py.File(os.path.expanduser(hdf5_path), mode='r')
        if sample_keys is None:
            sample_keys = list(self.file.keys())
        self.sample_keys = sample_keys
        
        self.include_coverage = include_coverage
        self.nocs_dims = nocs_dims

        self.aug_enable = aug_enable
        # self.aug_pipeline = iaa.Sometimes(aug_prob, iaa.Affine(
        #     translate_percent={
        #         "x": (-shift_range, shift_range), 
        #         "y": (-shift_range, shift_range)
        #     },
        #     rotate=(-rot_range,rot_range),
        # ))
    
        self.color_aug_pipeline = torchvision.transforms.Compose([
            torchvision.transforms.ColorJitter(brightness=0.5, contrast=0.5, saturation=0.5, hue=0.5),
            torchvision.transforms.RandomAdjustSharpness(0.5, 1.5),
        ])
        self.aug_pipeline = torchvision.transforms.Compose([
            torchvision.transforms.RandomAffine(degrees=180, translate=(0.2, 0.2), scale=(0.7, 1.3)),
        ])
        self.static_epoch_seed = static_epoch_seed

        self.static_epoch_seed = static_epoch_seed
        self.bins = bins
        self.ignore_index = ignore_index
    
    def __len__(self):
        return len(self.sample_keys)
    
    def __getitem__(self, idx: int) -> dict:
        key = self.sample_keys[idx]
        data_in = self.file[key]

        rgb = torch.tensor(data_in['pretransform_observations'][:3])
        nocs = torch.tensor(data_in['pretransform_observations'][4:7])

        #resize rgb and nocs to 128x128x3
        rgb = torchvision.transforms.functional.resize(rgb, (128, 128))
        nocs = torchvision.transforms.functional.resize(nocs, (128, 128))

        if self.aug_enable:
            if self.static_epoch_seed:
                rs = np.random.RandomState(seed=idx)
                self.aug_pipeline.seed_(rs)
           
            combined = torch.cat([rgb, nocs], dim=0)
            combined = self.aug_pipeline(combined)
            rgb, nocs = combined[:3], combined[3:]
            rgb = self.color_aug_pipeline(rgb)
        
        rgb = np.array(rgb).transpose(1, 2, 0)
        nocs = np.array(nocs).transpose(1, 2, 0)

        target = nocs[...,self.nocs_dims].copy()
        target[...,0] = np.abs(target[...,0] - 0.5) * 2
        labels = np.clip((target * self.bins).astype(np.int64),0,self.bins-1)
        mask = np.linalg.norm(nocs, axis=-1) < 1e-7
        labels[mask] = self.ignore_index

        data = {
            'input': np.moveaxis(rgb, -1, 0).astype(np.float32),
            'target': np.moveaxis(labels, -1, 0)
        }
        if self.include_coverage:
            data['coverage'] = np.array(
                data_in.attrs['postaction_coverage'],dtype=np.float32)
        data_torch = dict()
        for key, value in data.items():
            data_torch[key] = torch.from_numpy(value)
        return data_torch


class NOCSDataModule(pl.LightningDataModule):
    def __init__(self, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.split_sample_keys = None
    
    def prepare_data(self) -> None:
        kwargs = self.kwargs
        file = h5py.File(os.path.expanduser(kwargs['hdf5_path']), mode='r')
        required_keys = ['pretransform_observations']
        print('Reading task ids and sample keys.')

        n_valid = 0
        task_samples_map = defaultdict(list)
        for key in tqdm(file.keys()):
            data = file[key]
            is_valid = True
            for rk in required_keys:
                if rk not in data:
                    is_valid = False
                    print('Missing key: {}'.format(key))
            if is_valid:
                n_valid += 1
                task_name = '{:.5f}'.format(
                    np.sum(data['init_verts']),
                )
                task_samples_map[task_name].append(key)
        task_ids = np.array(list(task_samples_map.keys()))

        train_split = kwargs.get('train_split', 0.9)
        num_train = int(len(task_ids) * train_split)
        split_seed = kwargs.get('split_seed', 0)
        rs = np.random.RandomState(seed=split_seed)
        all_idxs = rs.permutation(len(task_ids))
        train_task_ids = task_ids[all_idxs[:num_train]]
        val_task_ids = task_ids[all_idxs[num_train:]]

        train_keys = list(chain.from_iterable(task_samples_map[x] for x in train_task_ids))
        val_keys = list(chain.from_iterable(task_samples_map[x] for x in val_task_ids))

        self.split_sample_keys = {
            'train': train_keys,
            'val': val_keys
        }
    
    def get_dataset(self, set_name: str, **kwargs):
        assert(set_name in ['train', 'val'])

        is_train = (set_name == 'train')
        dataset_kwargs = copy.deepcopy(self.kwargs)
        dataset_kwargs['sample_keys'] = self.split_sample_keys[set_name]
        dataset_kwargs['aug_enable'] = is_train
        dataset_kwargs['static_epoch_seed'] = not is_train
        dataset_kwargs.update(**kwargs)
        dataset = NOCSDataset(**dataset_kwargs)
        return dataset
    
    def get_dataloader(self, set_name: str):
        assert(set_name in ['train', 'val'])
        kwargs = self.kwargs
        batch_size = kwargs['batch_size']
        num_workers = kwargs['num_workers']

        is_train = (set_name == 'train')
        dataset = self.get_dataset(set_name)

        dataloader = DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=is_train,
            drop_last=is_train,
            num_workers=num_workers)
        return dataloader

    def train_dataloader(self):
        return self.get_dataloader('train')

    def val_dataloader(self):
        return self.get_dataloader('val')
