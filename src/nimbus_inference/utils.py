import os
import cv2
import json
import torch
import random
import numpy as np
import pandas as pd
import imageio as io
from copy import copy
from tqdm.autonotebook import tqdm
from joblib import Parallel, delayed
from joblib.externals.loky import get_reusable_executor
from skimage.segmentation import find_boundaries
from skimage.measure import regionprops_table
from pyometiff import OMETIFFReader
from pyometiff.omexml import OMEXML
from alpineer import io_utils, misc_utils
from typing import Callable
import tifffile
import zarr
import sys, os
import logging
import os, sys


class HidePrints:
    def __enter__(self):
        self._original_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._original_stdout


class LazyOMETIFFReader(OMETIFFReader):
    def __init__(self, fpath: str):
        """Lazy OMETIFFReader class that reads channels only when needed
        Args:
            fpath (str): path to ome.tif file
        """
        super().__init__(fpath)
        self.metadata = self.get_metadata()
        self.channels = self.get_channel_names()
        self.shape = self.get_shape()
    
    def get_metadata(self):
        """Get the metadata of the OME-TIFF file
        Returns:
            metadata (dict): metadata of the OME-TIFF file
        """
        with tifffile.TiffFile(str(self.fpath)) as tif:
            if tif.is_ome:
                omexml_string = tif.ome_metadata
                with HidePrints():
                    metadata = self.parse_metadata(omexml_string)
                return metadata
            else:
                raise ValueError("File is not an OME-TIFF file.")

    def get_channel_names(self):
        """Get the channel names of the OME-TIFF file
        Returns:
            channel_names (list): list of channel names
        """
        if hasattr(self, "metadata"):
            return list(self.metadata["Channels"].keys())
        else:
            return []
    
    def get_shape(self):
        """Get the shape of the OME-TIFF file array data
        Returns:
            shape (tuple): shape of the array data
        """
        with tifffile.imread(str(self.fpath), aszarr=True) as store:
            z = zarr.open(store, mode='r')
            shape = z.shape
        return shape

    def get_channel(self, channel_name: str):
        """Get an individual channel from the OME-TIFF file by name
        Args:
            channel_name (str): name of the channel
        Returns:
            channel (np.array): channel image
        """
        idx = self.channels.index(channel_name)
        with tifffile.imread(str(self.fpath), aszarr=True) as store:
            z = zarr.open(store, mode='r')
            # correct DimOrder, often DimOrder is TZCYX, but image is stored as CYX,
            # thus we remove the trailing dimensions
            dim_order = self.metadata["DimOrder"]
            dim_order = dim_order[-len(z.shape):]
            channel_idx = dim_order.find("C")
            slice_string = "z[" + ":," * channel_idx + str(idx) + "]"
            channel = eval(slice_string)
        return channel


class MultiplexDataset():
    def __init__(
            self, fov_paths: list, segmentation_naming_convention: Callable = None,
            include_channels: list = [], suffix: str = ".tiff", silent=False,
        ):
        """Multiplex dataset class that gives a common interface for data loading of multiplex
        datasets stored as individual channel images in folders or as multi-channel tiffs.
        Args:
            fov_paths (list): list of paths to fovs
            segmentation_naming_convention (function): function to get instance mask path from fov
            path
            suffix (str): suffix of channel images
            silent (bool): whether to print messages
        """
        self.fov_paths = fov_paths
        self.segmentation_naming_convention = segmentation_naming_convention
        self.suffix = suffix
        self.silent = silent
        self.include_channels = include_channels
        self.multi_channel = self.is_multi_channel_tiff(fov_paths[0])
        self.channels = self.get_channels()
        self.check_inputs()
        self.fovs = self.get_fovs()
        self.channels = self.filter_channels(self.channels)

    def filter_channels(self, channels):
        """Filter channels based on include_channels
        Args:
            channels (list): list of channel names
        Returns:
            channels (list): filtered list of channel names
        """
        if self.include_channels:
            return [channel for channel in channels if channel in self.include_channels]
        return channels

    def check_inputs(self):
        """check inputs for Nimbus model"""
        # check if all paths in fov_paths exists
        if not isinstance(self.fov_paths, (list, tuple)):
            self.fov_paths = [self.fov_paths]
        io_utils.validate_paths(self.fov_paths)
        if isinstance(self.include_channels, str):
            self.include_channels = [self.include_channels]
        misc_utils.verify_in_list(
            include_channels=self.include_channels, dataset_channels=self.channels
        )
        if not self.silent:
            print("All inputs are valid")

    def __len__(self):
        """Return the number of fovs in the dataset"""
        return len(self.fov_paths)
    
    def is_multi_channel_tiff(self, fov_path: str):
        """Check if fov is a multi-channel tiff
        Args:
            fov_path (str): path to fov
        Returns:
            multi_channel (bool): whether fov is multi-channel
        """
        multi_channel = False
        if fov_path.lower().endswith(("ome.tif", "ome.tiff")):
            self.img_reader = LazyOMETIFFReader(fov_path)
            if len(self.img_reader.shape) > 2:
                multi_channel = True
        return multi_channel
    
    def get_channels(self):
        """Get the channel names for the dataset"""
        if self.multi_channel:
            return self.img_reader.channels
        else:
            channels = [
                channel.replace(self.suffix, "") for channel in os.listdir(self.fov_paths[0]) \
                    if channel.endswith(self.suffix)
            ]
            return channels
    
    def get_fovs(self):
        """Get the fovs in the dataset"""
        return [os.path.basename(fov).replace(self.suffix, "") for fov in self.fov_paths]
    
    def get_channel(self, fov: str, channel: str):
        """Get a channel from a fov
        Args:
            fov (str): name of a fov
            channel (str): channel name
        Returns:
            channel (np.array): channel image
        """
        if self.multi_channel:
            return self.get_channel_stack(fov, channel)
        else:
            return self.get_channel_single(fov, channel)

    def get_channel_single(self, fov: str, channel: str):
        """Get a channel from a fov stored as a folder with individual channel images
        Args:
            fov (str): name of a fov
            channel (str): channel name
        Returns:
            channel (np.array): channel image
        """
        idx = self.fovs.index(fov)
        fov_path = self.fov_paths[idx]
        channel_path = os.path.join(fov_path, channel + self.suffix)
        channel = np.squeeze(io.imread(channel_path))
        return channel

    def get_channel_stack(self, fov: str, channel: str):
        """Get a channel from a multi-channel tiff
        Args:
            fov (str): name of a fov
            channel (str): channel name
            data_format (str): data format
        Returns:
            channel (np.array): channel image
        """
        idx = self.fovs.index(fov)
        fov_path = self.fov_paths[idx]
        self.img_reader = LazyOMETIFFReader(fov_path)
        return np.squeeze(self.img_reader.get_channel(channel))
    
    def get_segmentation(self, fov: str):
        """Get the instance mask for a fov
        Args:
            fov (str): name of a fov
        Returns:
            instance_mask (np.array): instance mask
        """
        idx = self.fovs.index(fov)
        fov_path = self.fov_paths[idx]
        instance_path = self.segmentation_naming_convention(fov_path)
        instance_mask = np.squeeze(io.imread(instance_path))
        return instance_mask


def prepare_input_data(mplex_img, instance_mask):
    """Prepares the input data for the segmentation model
    Args:
        mplex_img (np.array): multiplex image
        instance_mask (np.array): instance mask
    Returns:
        input_data (np.array): input data for segmentation model
    """
    mplex_img = mplex_img.astype(np.float32)
    edge = find_boundaries(instance_mask, mode="inner").astype(np.uint8)
    binary_mask = np.logical_and(edge == 0, instance_mask > 0).astype(np.float32)
    input_data = np.stack([mplex_img, binary_mask], axis=0)[np.newaxis,...] # bhwc
    return input_data


def segment_mean(instance_mask, prediction):
    """Calculates the mean prediction per instance
    Args:
        instance_mask (np.array): instance mask
        prediction (np.array): prediction
    Returns:
        uniques (np.array): unique instance ids
        mean_per_cell (np.array): mean prediction per instance
    """
    props_df = regionprops_table(
        label_image=instance_mask, intensity_image=prediction,
        properties=['label' ,'intensity_mean']
    )
    return props_df


def test_time_aug(
        input_data, channel, app, normalization_dict, rotate=True, flip=True, batch_size=4
    ):
    """Performs test time augmentation
    Args:
        input_data (np.array): input data for segmentation model, mplex_img and binary mask
        channel (str): channel name
        app (Nimbus): segmentation model
        normalization_dict (dict): dict with channel names as keys and norm factors  as values
        rotate (bool): whether to rotate
        flip (bool): whether to flip
        batch_size (int): batch size
    Returns:
        seg_map (np.array): predicted segmentation map
    """
    forward_augmentations = []
    backward_augmentations = []
    if not isinstance(input_data, torch.Tensor):
        input_data = torch.tensor(input_data)
    if rotate:
        for k in [0,1,2,3]:
            forward_augmentations.append(lambda x: torch.rot90(x, k=k, dims=[2,3]))
            backward_augmentations.append(lambda x: torch.rot90(x, k=-k, dims=[2,3]))
    if flip:
        forward_augmentations += [
            lambda x: torch.flip(x, [2]),
            lambda x: torch.flip(x, [3])
        ]
        backward_augmentations += [
            lambda x: torch.flip(x, [2]),
            lambda x: torch.flip(x, [3])
        ]
    input_batch = []
    for forw_aug in forward_augmentations:
        input_data_tmp = forw_aug(input_data).numpy() # bhwc
        input_batch.append(np.concatenate(input_data_tmp))
    input_batch = np.stack(input_batch, 0)
    seg_map = app.predict_segmentation(
        input_batch,
        preprocess_kwargs={
            "normalize": True,
            "marker": channel,
            "normalization_dict": normalization_dict},
        )
    seg_map = torch.from_numpy(seg_map)
    tmp = []
    for backw_aug, seg_map_tmp in zip(backward_augmentations, seg_map):
        seg_map_tmp = backw_aug(seg_map_tmp[np.newaxis,...])
        seg_map_tmp = np.squeeze(seg_map_tmp)
        tmp.append(seg_map_tmp)
    seg_map = np.stack(tmp, 0)
    seg_map = np.mean(seg_map, axis = 0)
    return seg_map


def predict_fovs(
        nimbus, dataset: MultiplexDataset, normalization_dict: dict, output_dir: str,
        suffix: str="tiff", save_predictions: bool=True, half_resolution: bool=False,
        batch_size: int=4, test_time_augmentation: bool=True
    ):
    """Predicts the segmentation map for each mplex image in each fov
    Args:
        nimbus (Nimbus): nimbus object
        dataset (MultiplexDataset): dataset object
        normalization_dict (dict): dict with channel names as keys and norm factors  as values
        output_dir (str): path to output dir
        suffix (str): suffix of mplex images
        save_predictions (bool): whether to save predictions
        half_resolution (bool): whether to use half resolution
        batch_size (int): batch size
        test_time_augmentation (bool): whether to use test time augmentation
    Returns:
        cell_table (pd.DataFrame): cell table with predicted confidence scores per fov and cell
    """
    fov_dict_list = []
    for fov_path, fov in zip(dataset.fov_paths, dataset.fovs):
        print(f"Predicting {fov_path}...")
        out_fov_path = os.path.join(
            os.path.normpath(output_dir), os.path.basename(fov_path)
        )
        df_fov = pd.DataFrame()
        instance_mask = dataset.get_segmentation(fov)
        for channel_name in tqdm(dataset.channels):
            mplex_img = dataset.get_channel(fov, channel_name)
            input_data = prepare_input_data(mplex_img, instance_mask)
            if half_resolution:
                scale = 0.5
                input_data = np.squeeze(input_data)
                _, h,w = input_data.shape
                img = cv2.resize(input_data[0], [int(h*scale), int(w*scale)])
                binary_mask = cv2.resize(
                    input_data[1], [int(h*scale), int(w*scale)], interpolation=0
                )
                input_data = np.stack([img, binary_mask], axis=0)[np.newaxis,...]
            if test_time_augmentation:
                prediction = test_time_aug(
                    input_data, channel_name, nimbus, normalization_dict, batch_size=batch_size
                )
            else:
                prediction = nimbus.predict_segmentation(
                    input_data,
                    preprocess_kwargs={
                        "normalize": True, "marker": channel_name,
                        "normalization_dict": normalization_dict
                    },
                )
            prediction = np.squeeze(prediction)
            if half_resolution:
                prediction = cv2.resize(prediction, (h, w))
            df = pd.DataFrame(segment_mean(instance_mask, prediction))
            if df_fov.empty:
                df_fov["label"] = df["label"]
                df_fov["fov"] = os.path.basename(fov_path)
            df_fov[channel_name] = df["intensity_mean"]
            if save_predictions:
                os.makedirs(out_fov_path, exist_ok=True)
                pred_int = (prediction*255.0).astype(np.uint8)
                io.imwrite(
                    os.path.join(out_fov_path, channel_name + suffix), pred_int, photometric="minisblack",
                    # compress=0, 
                )
        fov_dict_list.append(df_fov)
    cell_table = pd.concat(fov_dict_list, ignore_index=True)
    return cell_table


def nimbus_preprocess(image, **kwargs):
    """Preprocess input data for Nimbus model.
    Args:
        image: array to be processed
    Returns:
        np.array: processed image array
    """
    output = np.copy(image.astype(np.float32))
    if len(image.shape) != 4:
        raise ValueError("Image data must be 4D, got image of shape {}".format(image.shape))

    normalize = kwargs.get('normalize', True)
    if normalize:
        marker = kwargs.get('marker', None)
        normalization_dict = kwargs.get('normalization_dict', {})
        if marker in normalization_dict.keys():
            norm_factor = normalization_dict[marker]
        else:
            print("Norm_factor not found for marker {}, calculating directly from the image. \
            ".format(marker))
            norm_factor = np.quantile(output[..., 0], 0.999)
        # normalize only marker channel in chan 0 not binary mask in chan 1
        output[..., 0] /= norm_factor
        output = output.clip(0, 1)
    return output


def calculate_normalization(dataset: MultiplexDataset, quantile: float):
    """Calculates the normalization values for a given ome file
    Args:
        dataset (MultiplexDataset): dataset object
        quantile (float): quantile to use for normalization
    Returns:
        normalization_values (dict): dict with channel names as keys and norm factors  as values
    """
    normalization_values = {}
    for channel in dataset.channels:
        mplex_img = dataset.get_channel(dataset.fovs[0], channel)
        mplex_img = mplex_img.astype(np.float32)
        foreground = mplex_img[mplex_img > 0]
        normalization_values[channel] = np.quantile(foreground, quantile)
    return normalization_values


def prepare_normalization_dict(
        dataset: MultiplexDataset, output_dir: str, quantile: float=0.999, n_subset: int=10,
        n_jobs: int=1, output_name: str="normalization_dict.json"
    ):
    """Prepares the normalization dict for a list of ome.tif fovs
    Args:
        MultiplexDataset (list): list of paths to fovs
        output_dir (str): path to output directory
        quantile (float): quantile to use for normalization
        n_subset (int): number of fovs to use for normalization
        n_jobs (int): number of jobs to use for joblib multiprocessing
        output_name (str): name of output file
    Returns:
        normalization_dict (dict): dict with channel names as keys and norm factors  as values
    """
    normalization_dict = {}
    fov_paths = copy(dataset.fov_paths)
    if n_subset is not None:
        random.shuffle(fov_paths)
        fov_paths = fov_paths[:n_subset]
    print("Iterate over fovs...")
    if n_jobs > 1:
        normalization_values = Parallel(n_jobs=n_jobs)(
            delayed(calculate_normalization)(
                MultiplexDataset(
                    [fov_path], dataset.segmentation_naming_convention, dataset.channels,
                    dataset.suffix, True
                ), quantile)
            for fov_path in fov_paths
        )
    else:
        normalization_values = [
            calculate_normalization(
                MultiplexDataset(
                    [fov_path], dataset.segmentation_naming_convention, dataset.channels,
                    dataset.suffix, True
                ), quantile)
            for fov_path in fov_paths
        ]
    for norm_dict in normalization_values:
        for channel, normalization_value in norm_dict.items():
            if channel not in normalization_dict:
                normalization_dict[channel] = []
            normalization_dict[channel].append(normalization_value)
    if n_jobs > 1:
        get_reusable_executor().shutdown(wait=True)
    for channel in normalization_dict.keys():
        normalization_dict[channel] = np.mean(normalization_dict[channel])
    # save normalization dict
    with open(os.path.join(output_dir, output_name), 'w') as f:
        json.dump(normalization_dict, f)
    return normalization_dict
