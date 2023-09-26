import os
import sys
import copy
import glob
import random
import numpy as np
import pandas as pd
import utils.config as cfg
from typing import List, Tuple
from utils.poses import xyz_ypr2m
import datasets.velodyne as velodyne
from utils.tools import find_nearest_ndx, check_in_train_set, check_in_test_set
import matplotlib.pyplot as plt
from sklearn.neighbors import KDTree
from scipy.spatial.transform import Rotation
from torch.utils.data import Dataset, ConcatDataset, DataLoader


# calibrate the lidar data of oxford radar robotcar dataset
def get_oxford_radar_calib():
    calib = np.eye(4)
    calib[0,3] = -1.7132
    calib[1,3] = 0.1181
    calib[2,3] = 1.1948
    calib[:3,:3] = Rotation.from_euler('xyz',[-0.0125,0.0400,0.0050],degrees=True).as_matrix()
    return calib


# concantate point clouds generated by the left and right lidar
def pc_concantate(pc_left, pc_right, extrinsics_dir):
    """
    input pc_left: Nx4
    input pc_right: Mx4
    input extrinsics_dir: path to extrinsics
    return pc_cat: (N+M)x4
    """
    # transpose pc_left and pc_right
    pc_left = pc_left.transpose(1, 0)
    pc_right = pc_right.transpose(1, 0)
    # load extrinsics of left lidar
    left = np.loadtxt(os.path.join(extrinsics_dir, 'velodyne_left.txt'), delimiter=' ', dtype=np.float32)
    roll, pitch, yaw = left[3], left[4], left[5]
    # convert euler angles to rotation matrix
    left_T = np.eye(4)
    R_x = np.array([[1, 0, 0],
                    [0, np.cos(roll), -np.sin(roll)],
                    [0, np.sin(roll), np.cos(roll)]])
    R_y = np.array([[np.cos(pitch), 0, np.sin(pitch)],
                    [0, 1, 0],
                    [-np.sin(pitch), 0, np.cos(pitch)]])
    R_z = np.array([[np.cos(yaw), -np.sin(yaw), 0],
                    [np.sin(yaw), np.cos(yaw), 0],
                    [0, 0, 1]])
    R = np.dot(R_z, np.dot(R_y, R_x))
    # translation
    t = np.array(left[:3])
    left_T[:3, :3] = R
    left_T[:3, 3] = t
    intensity_array = copy.deepcopy(pc_left[3,:])
    pc_left[3,:] = 1
    pc_left_ed = np.dot(left_T, pc_left)  
    pc_left[3,:] = intensity_array
    pc_left_ed[3,:] = intensity_array

    # load extrinsics of right lidar
    right = np.loadtxt(os.path.join(extrinsics_dir, 'velodyne_right.txt'), delimiter=' ', dtype=np.float32)
    roll, pitch, yaw = right[3], right[4], right[5]
    # convert euler angles to rotation matrix
    right_T = np.eye(4)
    R_x = np.array([[1, 0, 0],
                    [0, np.cos(roll), -np.sin(roll)],
                    [0, np.sin(roll), np.cos(roll)]])
    R_y = np.array([[np.cos(pitch), 0, np.sin(pitch)],
                    [0, 1, 0],
                    [-np.sin(pitch), 0, np.cos(pitch)]])
    R_z = np.array([[np.cos(yaw), -np.sin(yaw), 0],
                    [np.sin(yaw), np.cos(yaw), 0],
                    [0, 0, 1]])
    R = np.dot(R_z, np.dot(R_y, R_x))
    # translation
    t = np.array(right[:3])
    right_T[:3, :3] = R
    right_T[:3, 3] = t
    intensity_array = copy.deepcopy(pc_right[3,:])
    pc_right[3,:] = 1
    pc_right_ed = np.dot(right_T, pc_right)  
    pc_right[3,:] = intensity_array
    pc_right_ed[3,:] = intensity_array
    pc_cat = np.concatenate((pc_left_ed, pc_right_ed), axis=1)
    pc_cat = pc_cat.transpose(1, 0)
    pc_cat = pc_cat.astype(np.float32)
    return pc_cat


# load point cloud from bin or raw png file
def load_pc_oxford_radar(scan_path):
    n_vec = 4
    if scan_path.endswith('.bin'):
        pc = np.fromfile(scan_path, dtype=np.float32)
        pc = np.reshape(pc, (-1, n_vec))
        # print(pc.shape)
        # print('------ debug ------', np.min(pc[:,0]), np.max(pc[:,0]), np.min(pc[:,1]), np.max(pc[:,1]), np.min(pc[:,2]), np.max(pc[:,2]))
    elif scan_path.endswith('.png'):
        ranges, intensities, angles, approximate_timestamps = velodyne.load_velodyne_raw(scan_path)
        pc = velodyne.velodyne_raw_to_pointcloud(ranges, intensities, angles)
        pc = pc.reshape((-1, n_vec))
    pc = pc.astype(np.float32)
    return pc


class OxfordRadarPointCloudLoader:
    def __init__(self, remove_zero_points: bool = True, remove_ground_plane: bool = True, ground_plane_level: float = -13.0):
        # remove_zero_points: remove points with all zero coordinates
        # remove_ground_plane: remove points on ground plane level and below
        # ground_plane_level: ground plane level
        self.remove_zero_points = remove_zero_points
        self.remove_ground_plane = remove_ground_plane
        self.ground_plane_level = ground_plane_level

    def read_pc(self, file_pathname: str):
        # Reads the point cloud and preprocess (optional removal of zero points and points on the ground plane
        pc = load_pc_oxford_radar(file_pathname)
        if self.remove_zero_points:
            mask = np.all(np.isclose(pc, 0), axis=1)
            pc = pc[~mask]

        if self.remove_ground_plane:
            mask = pc[:, 2] > self.ground_plane_level
            pc = pc[mask]        
        
        pc = np.unique(pc, axis=0) # remove duplicate points  

        return pc

    def read_pc_cat(self, left_file_pathname: str, right_file_pathname: str, extrinsics_dir: str):
        # Reads the point cloud and preprocess (optional removal of zero points and points on the ground plane
        pc_left = load_pc_oxford_radar(left_file_pathname)
        pc_right = load_pc_oxford_radar(right_file_pathname)
        pc = pc_concantate(pc_left, pc_right, extrinsics_dir)
        if self.remove_zero_points:
            mask = np.all(np.isclose(pc, 0), axis=1)
            pc = pc[~mask]

        if self.remove_ground_plane:
            mask = pc[:, 2] > self.ground_plane_level
            pc = pc[mask]    

        pc = np.unique(pc, axis=0) # remove duplicate points      

        return pc
                
    def normalize_pc(self, pc, xbound: float = cfg.point_cloud["x_bound"][1], ybound: float = cfg.point_cloud["y_bound"][1], zbound: float = 15.):
        # Normalize the point cloud
        pc = np.array(pc, dtype=np.float32)
        pc = pc[:,:3]

        mask = (np.abs(pc[:,0]) < xbound) * (np.abs(pc[:,1]) < ybound) * (pc[:,2] > self.ground_plane_level) * (pc[:,2] < zbound)

        pc = pc[mask]
        pc[:,0] = pc[:,0] / xbound
        pc[:,1] = pc[:,1] / ybound
        pc[:,2] = pc[:,2] / zbound 

        return pc   


class OxfordRadarSequence(Dataset):
    """
    Single Oxford Radar sequence indexed as a single dataset containing point clouds and poses
    """
    def __init__(self, dataset_root: str, sequence_name: str, split: str = 'train', lidar: str = 'all', sampling_distance: float = 0.2):
        assert os.path.exists(dataset_root), f'Cannot access dataset root: {dataset_root}'
        assert split in ['train', 'test', 'all']
        assert lidar in ['left', 'right', 'all']
        self.dataset_root = dataset_root
        self.sequence_name = sequence_name
        self.sequence_path = os.path.join(self.dataset_root, self.sequence_name)   
        assert os.path.exists(self.sequence_path), f'Cannot access sequence: {self.sequence_path}'          
        self.split = split
        self.lidar = lidar
        self.sampling_distance = sampling_distance
        # Maximum discrepancy between timestamps of LiDAR scan and global pose in seconds
        self.pose_time_tolerance = 1.
        self.pose_file = os.path.join(self.sequence_path, 'ins.csv')
        assert os.path.exists(self.pose_file), f'Cannot access global pose file: {self.pose_file}'
        self.lidar_path = os.path.join(self.sequence_path, 'velodyne_' + self.lidar)
        # assert os.path.exists(self.lidar_path), f'Cannot access lidar scans: {self.lidar_path}'      
        # self.lidar_files = sorted(glob.glob(os.path.join(self.lidar_path, '*.bin')))
        self.pc_loader = OxfordRadarPointCloudLoader()
        self.timestamps, self.filepaths, self.poses, self.xys = self.get_scan_poses()
        assert len(self.filepaths) == len(self.poses), f'Number of lidar file paths and poses do not match: {len(self.filepaths)} vs {len(self.poses)}'
        print(f'{len(self.filepaths)} scans in {sequence_name}-{split}-{lidar}')
        # Build a kdtree based on X, Y position
        self.kdtree = KDTree(self.xys)

    def __len__(self):
        return len(self.filepaths)

    def __getitem__(self, ndx):
        pc_filepath = self.filepaths[ndx]
        pc = self.load_pc(pc_filepath)
        return {'ts': self.timestamps[ndx], 'pc': pc, 'position': self.xys[ndx], 'pose': self.poses[ndx]}
    
    def load_pc(self, scan_path):
        # Load point cloud from file
        pc = self.pc_loader.read_pc(scan_path)
        return pc
    
    def load_pcs(self, scan_paths):
        # Load point clouds from files
        pcs = []
        for scan_path in scan_paths:
            pc = self.load_pc(scan_path)
            if len(pc) == 0:
                continue
            pcs.append(pc)
        pcs = np.array(pcs)
        return pcs  
    
    def normalize_pc(self, pc):
        # Normalize the point cloud
        pc = self.pc_loader.normalize_pc(pc)
        return pc
    
    def normalize_pcs(self, pcs):
        # Normalize point clouds
        pcs = [self.normalize_pc(pc) for pc in pcs]
        return pcs
        
    def get_scan_poses(self):
        # Read global poses from .csv file and link each lidar_scan with the nearest pose
        # threshold: timestamp threshold in seconds
        # Returns a dictionary with (4, 4) pose matrix indexed by a timestamp (as integer)
        pose_data = pd.read_csv(self.pose_file, header=0, low_memory=False)

        n = len(pose_data)
        print(f'Number of global poses: {n}')
        system_timestamps = np.zeros((n,), dtype=np.int64)
        poses = np.zeros((n, 4, 4), dtype=np.float32)       # 4x4 pose matrix
        calib = get_oxford_radar_calib()

        for ndx in range(n):
            row = pose_data.iloc[ndx]
            assert len(row) == 15, f'Invalid line in global poses file: {row}'
            ts = int(row['timestamp'])
            x = float(row['northing'])
            y = float(row['easting'])
            z = float(row['down'])
            roll = float(row['roll'])
            pitch = float(row['pitch'])
            yaw = float(row['yaw'])            
            # transform to se3 matrix
            se3 = xyz_ypr2m(x, y, z, yaw, pitch, roll)
            # add to system_timestamps and poses
            system_timestamps[ndx] = int(ts)
            poses[ndx] = se3 @ calib
            # poses[ndx] = se3


        # Ensure timestamps and poses are sorted in ascending order
        sorted_ndx = np.argsort(system_timestamps, axis=0)
        system_timestamps = system_timestamps[sorted_ndx]
        poses = poses[sorted_ndx]

        # List LiDAR scan timestamps
        if self.lidar == 'all':
            if not os.path.exists(self.lidar_path):
                os.makedirs(self.lidar_path)
                print(f'Created directory: {self.lidar_path}')
            left_lidar_path = os.path.join(self.sequence_path, 'velodyne_left')
            right_lidar_path = os.path.join(self.sequence_path, 'velodyne_right')
            left_lidar_timestamps = [int(os.path.splitext(f)[0]) for f in os.listdir(left_lidar_path)]
            right_lidar_timestamps = [int(os.path.splitext(f)[0]) for f in os.listdir(right_lidar_path)]
            left_lidar_timestamps.sort()
            right_lidar_timestamps.sort()
            print(f'Number of left lidar scans: {len(left_lidar_timestamps)}')
            print(f'Number of right lidar scans: {len(right_lidar_timestamps)}')
            for ndx, lidar_ts in enumerate(left_lidar_timestamps):
                # Find index of the closest timestamp
                closest_ts_ndx = find_nearest_ndx(lidar_ts, right_lidar_timestamps)
                delta = abs(right_lidar_timestamps[closest_ts_ndx] - lidar_ts)
                # Timestamp is in nanoseconds = 1e-9 second
                if delta > self.pose_time_tolerance * 1000000000:
                    continue                
                # Concatenate left and right lidar scans and save to .bin file
                bin_filepath = os.path.join(self.lidar_path, f'{lidar_ts}.bin')
                if not os.path.exists(bin_filepath):
                    left_lidar_filepath = os.path.join(left_lidar_path, f'{lidar_ts}.png')
                    right_lidar_filepath = os.path.join(right_lidar_path, f'{right_lidar_timestamps[closest_ts_ndx]}.png')
                    pc_cat = self.pc_loader.read_pc_cat(left_lidar_filepath, right_lidar_filepath, self.dataset_root)
                    pc_cat.tofile(bin_filepath)
                    print(f'Save the concatenated lidar scan from {left_lidar_filepath} and {right_lidar_filepath} to {bin_filepath}')
                
        all_lidar_timestamps = [int(os.path.splitext(f)[0]) for f in os.listdir(self.lidar_path)]
        all_lidar_timestamps.sort()
        print(f'Number of global scans: {len(all_lidar_timestamps)}')
        
        lidar_timestamps = []
        lidar_filepaths = []
        lidar_poses = []
        count_rejected = 0

        for ndx, lidar_ts in enumerate(all_lidar_timestamps):
            # Find index of the closest timestamp
            closest_ts_ndx = find_nearest_ndx(lidar_ts, system_timestamps)
            delta = abs(system_timestamps[closest_ts_ndx] - lidar_ts)
            # Timestamp is in nanoseconds = 1e-9 second
            if delta > self.pose_time_tolerance * 1000000000:
                # Reject point cloud without corresponding pose
                count_rejected += 1
                continue
            
            if self.lidar == 'all':
                lidar_filepaths.append(os.path.join(self.lidar_path, f'{lidar_ts}.bin'))
            else:
                lidar_filepaths.append(os.path.join(self.lidar_path, f'{lidar_ts}.png'))
            lidar_timestamps.append(lidar_ts)
            lidar_poses.append(poses[closest_ts_ndx])
        
        lidar_timestamps = np.array(lidar_timestamps, dtype=np.int64)
        lidar_filepaths = np.array(lidar_filepaths)
        lidar_poses = np.array(lidar_poses, dtype=np.float32)     # 4x4 pose matrix
        lidar_xys = lidar_poses[:, :2, 3]                         # 2D position

        # split data into train / test set
        if self.split != 'all':      
            if self.split == 'train':
                mask = check_in_train_set(lidar_xys, dataset='oxford_radar')
            elif self.split == 'test':
                mask = check_in_test_set(lidar_xys, dataset='oxford_radar')
            lidar_timestamps = lidar_timestamps[mask]
            lidar_filepaths = lidar_filepaths[mask]
            lidar_poses = lidar_poses[mask]
            lidar_xys = lidar_xys[mask]

        # Sample lidar scans             
        prev_position = None
        mask = []
        for ndx, position in enumerate(lidar_xys):
            if prev_position is None:
                mask.append(ndx)
                prev_position = position
            else:
                displacement = np.linalg.norm(prev_position - position)
                if displacement > self.sampling_distance:
                    mask.append(ndx)
                    prev_position = position
        
        lidar_timestamps = lidar_timestamps[mask]
        lidar_filepaths = lidar_filepaths[mask]
        lidar_poses = lidar_poses[mask]
        lidar_xys = lidar_xys[mask]

        print(f'{len(lidar_timestamps)} scans with valid pose, {count_rejected} rejected due to unknown pose')
        return lidar_timestamps, lidar_filepaths, lidar_poses, lidar_xys

    def find_neighbours_ndx(self, position, radius):
        # Returns indices of neighbourhood point clouds for a given position
        assert position.ndim == 1
        assert position.shape[0] == 2
        # Reshape into (1, 2) axis
        position = position.reshape(1, -1)
        neighbours = self.kdtree.query_radius(position, radius)[0]
        return neighbours.astype(np.int32)


class OxfordRadarSequences(Dataset):
    """
    Multiple Oxford Radar sequences indexed as a single dataset containing point clouds and poses
    """
    def __init__(self, dataset_root: str, sequence_names: List[str], split: str = 'train', lidar: str = 'all', sampling_distance: float = 0.2):
        assert os.path.exists(dataset_root), f'Cannot access dataset root: {dataset_root}'
        assert split in ['train', 'test', 'all']
        assert lidar in ['left', 'right', 'all']
        self.dataset_root = dataset_root
        self.sequence_names = sequence_names
        self.split = split
        self.lidar = lidar
        self.sampling_distance = sampling_distance
        
        # Load all sequences
        sequences = []
        for sequence_name in sequence_names:
            sequence = OxfordRadarSequence(dataset_root, sequence_name, split, lidar, sampling_distance)
            sequences.append(sequence)
        self.dataset = ConcatDataset(sequences)
        self.pc_loader = sequences[0].pc_loader

        # Concatenate all sequences
        self.timestamps = np.concatenate([s.timestamps for s in sequences])
        self.filepaths = np.concatenate([s.filepaths for s in sequences])
        self.poses = np.concatenate([s.poses for s in sequences])
        self.xys = np.concatenate([s.xys for s in sequences])
        assert len(self.filepaths) == len(self.poses), f'Number of lidar file paths and poses do not match: {len(self.filepaths)} vs {len(self.poses)}'
        print(f'{len(self.filepaths)} scans in {sequence_names}-{split}-{lidar}')

        # Build a kdtree based on X, Y position
        self.kdtree = KDTree(self.xys)

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, ndx):
        return self.dataset[ndx]

    def load_pc(self, scan_path):
        # Load point cloud from file
        pc = self.pc_loader.read_pc(scan_path)
        return pc
    
    def load_pcs(self, scan_paths):
        # Load point clouds from files
        pcs = []
        for scan_path in scan_paths:
            pc = self.load_pc(scan_path)
            if len(pc) == 0:
                continue
            pcs.append(pc)
        pcs = np.array(pcs)
        return pcs  
    
    def normalize_pc(self, pc):
        # Normalize the point cloud
        pc = self.pc_loader.normalize_pc(pc)
        return pc
    
    def normalize_pcs(self, pcs):
        # Normalize point clouds
        pcs = [self.normalize_pc(pc) for pc in pcs]
        return pcs

    def find_neighbours_ndx(self, position, radius):
        # Returns indices of neighbourhood point clouds for a given position
        assert position.ndim == 1
        assert position.shape[0] == 2
        # Reshape into (1, 2) axis
        position = position.reshape(1, -1)
        neighbours = self.kdtree.query_radius(position, radius)[0]
        return neighbours.astype(np.int32)


if __name__ == "__main__":
    # load dataset
    base_path = './data/OxfordRadar/'
    folder = '2019-01-11-13-24-51' # sequence folder for training    
    train_dataset = OxfordRadarSequence(base_path, folder, split='train')
    test_dataset = OxfordRadarSequence(base_path, folder, split='test')
    train_positions = train_dataset.xys
    test_positions = test_dataset.xys
    # plot the splited results
    plt.scatter(train_positions[:,0], train_positions[:,1], c='r', s=0.1)
    plt.scatter(test_positions[:,0], test_positions[:,1], c='b', s=0.1)
    plt.xlabel('X')
    plt.ylabel('Y')
    plt.legend(['train', 'test'])
    plt.title(f'{folder} Trajectory Split')
    plt.savefig(f'{folder}_split.png')
    plt.show()
