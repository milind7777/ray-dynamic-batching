from scheduler import (
    NexusScheduler
)

import ray
import time
import copy
import torch
import itertools
import traceback
import logging
import os, csv, json
from collections import deque
from datetime import datetime, timedelta
from ray.util.queue import Queue as RayQueue
import threading
from threading import Lock, Thread
from queue import Queue, Empty
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

models_config = {
    'vit': {'SLO': 50, 'base_rate':0},        # (model_name, SLO, initial_rate)
    'resnet': {'SLO': 400, 'base_rate': 0},
    'shufflenet': {'SLO': 30, 'base_rate': 0},
    'efficientnet': {'SLO': 40, 'base_rate': 0}
}

class BatchProfiler:
    """
    Handles batch profiling data loading and management.
    
    Loads and manages model batch profiling data from CSV files containing
    performance metrics like latency and memory usage at different batch sizes.
    """
    @staticmethod
    def load_csv_to_dict(file_path: str) -> Dict[int, Dict[str, float]]:
        """Load CSV data into dictionary"""
        column_list = ['avg_latency_ms', 'peak_memory_mb']
        result = {}
        try:
            with open(file_path, 'r') as csvfile:
                reader = csv.DictReader(csvfile)
                headers = reader.fieldnames
                key_column = headers[0] if headers else None
                for row in reader:
                    key = int(row[key_column])
                    result[key] = {header: float(row[header]) 
                                 for header in headers[1:] 
                                 if header in column_list}
        except (FileNotFoundError, ValueError) as e:
            logging.error(f"Error loading batch profile from {file_path}: {e}")
            return {}
        return result

class WorkloadGenerator:
    """Generates synthetic workload for testing"""
    def __init__(self, scheduler: NexusScheduler, model_patterns: dict = {}, pattern_period: float = 80.0):
        self.scheduler = scheduler
        self.patterns  = model_patterns
        self.pattern_period = pattern_period

    def _start_load(self):
        for model in self.patterns:
            threading.Thread(target=self._run_pattern(model, self.patterns[model]), daemon=True).start()

    def _run_pattern(self, model_name: str, pattern: dict):
        start_time = time.time()
        while True:
            elapsed_time = (time.time() - start_time)
            if elapsed_time > self.pattern_period:
                break

            if pattern['type'] == 'slope':
                if elapsed_time < 20:
                    rate = (elapsed_time) * pattern['slope']
                # elif elapsed_time < 40:
                #     rate = 20 * pattern['slope']
                # elif elapsed_time < 60:
                #     rate = 20 * pattern['slope'] + (elapsed_time - 40) * pattern['slope']
                # elif elapsed_time < 80:
                #     rate = 40 * pattern['slope']
                
                second_start_time = time.time()
                for i in range(int(rate)):
                    input_tensor = torch.randn(3, 224, 224)
                    self.scheduler.submit_request(model_name, str(model_name) + str(time.time()), input_tensor)
                second_end_time = time.time()

            time_left = 1 - (second_end_time - second_start_time)
            # print(f"Time it took to send register requests: {second_end_time - second_start_time}")
            if time_left>0:
                time.sleep(time_left)
        
def main():
    # Load batch profiles
    profiling_dir = "../profiling"

    model_files = {
        'vit': 'vit_g16_20241123_154354_summary.csv',
        'resnet': 'resnet50_20241117_154052_summary.csv',
        'shufflenet': 'shufflenet_20241123_104115_summary.csv',
        'efficientnet': 'efficientnetv2_20241123_125206_summary.csv'
    }

        # Initialize batch profiler and load profiles
    profiler = BatchProfiler()
    batching_profile = {}
    
    for model_name, filename in model_files.items():
        file_path = os.path.join(profiling_dir, filename)
        profile = profiler.load_csv_to_dict(file_path)
        if profile:
            batching_profile[model_name] = profile
            logging.info(f"Loaded profile for {model_name}")
        else:
            logging.error(f"Failed to load profile for {model_name}")
            return

    # Initialize Ray with specific GPU configuration
    ray.init(
        runtime_env={
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": "0,1",
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                "RAY_DISABLE_MEMORY_MONITOR": "1"  # Prevent Ray from limiting GPU memory
            }
        },
        num_gpus=2
    )

    scheduler = NexusScheduler(batching_profile, monitoring_interval=1)
    scheduler.start_monitoring()

    model_patterns = {
        'resnet': {
            'type': 'slope',
            'base': 100,
            'final': 100,
            'slope': 1
        }
    }

    time.sleep(10)

    test1 = WorkloadGenerator(scheduler, model_patterns)
    test1._start_load()

if __name__ == '__main__':
    main()