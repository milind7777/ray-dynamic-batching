import ray
import time
import copy
import torch
from dataclasses import dataclass
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
from multiprocessing import shared_memory
import numpy as np
from nexus import (
    session,
    node,
    nexus
)

SLO_hack = 2.2

models_config = {
    'vit': {'SLO': 50, 'base_rate':1000},        # (model_name, SLO, initial_rate)
    'resnet': {'SLO': 200, 'base_rate': 2000},
    'shufflenet': {'SLO': 30, 'base_rate': 1500},
    'efficientnet': {'SLO': 40, 'base_rate': 1200}
}

import torchvision.models as models
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
model_registry = {
    'vit': models.vit_b_16(pretrained=True),  # Using direct torchvision import
    'shuffle': models.shufflenet_v2_x1_0(pretrained=True),
    'resnet': models.resnet50(pretrained=True)
}

class TestResultLogger:
    """Handles test result storage and logging"""
    def __init__(self, base_dir: str = "test_results"):
        self.base_dir = Path(base_dir)
        self.test_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.test_dir = self.base_dir / self.test_timestamp
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
    def log_metrics(self, test_name: str, metrics: dict):
        """Log metrics to JSON file"""
        file_path = self.test_dir / f"{test_name}_metrics.json"
        with open(file_path, 'w') as f:
            json.dump(metrics, f, indent=2, default=str)

    def log_changes(self, test_name: str, changes: list):
        """Log schedule changes to CSV"""
        file_path = self.test_dir / f"{test_name}_changes.csv"
        if changes:
            keys = changes[0].keys()
            with open(file_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(changes)

    def log_node_state(self, test_name: str, nodes: dict, timestamp: str):
        """Log node state to text file"""
        file_path = self.test_dir / f"{test_name}_nodes.txt"
        with open(file_path, 'a') as f:
            f.write(f"\nNode State at {timestamp}\n")
            f.write("=" * 50 + "\n")
            for model_name, model_nodes in nodes.items():
                f.write(f"\nModel: {model_name}\n")
                for i, n in enumerate(model_nodes):
                    f.write(f"\nNode {i+1}:\n")
                    f.write(f"Duty Cycle: {n.duty_cycle}ms\n")
                    f.write(f"Occupancy: {n.get_occupancy()*100:.2f}%\n")
                    for s, occ in n.node_sessions:
                        f.write(f"Session: {s.model_name}, "
                              f"Rate: {s.request_rate:.2f}, "
                              f"Batch: {s.batch_size}\n")
            f.write("\n" + "=" * 50 + "\n")

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
    
class RequestTracker:
    """
    Tracks and calculates request rates for models over a specified time window.
    
    The tracker maintains a count of requests within a sliding window and calculates
    the current request rate. Thread-safe implementation using locks.
    
    Attributes:
        window_size (float): Size of the sliding window in seconds (default: 60.0)
        request_count (int): Number of requests in current window
        last_reset (datetime): Timestamp of last window reset
        _total_requests (int): Total number of requests processed since initialization
    """
     
    def __init__(self, window_size: float = 60.0):
        """
        Initialize RequestTracker with specified window size.
        
        Args:
            window_size (float): Duration of sliding window in seconds
        """
        self.window_size = window_size
        self.request_count = 0
        self.last_reset = datetime.now()
        self.lock = Lock()
        self._total_requests = 0  # Track total requests for metrics

    def record_request(self) -> None:
        """
        Record a single request occurrence.
        Thread-safe increment of request counters.
        """
        with self.lock:
            self.request_count += 1
            self._total_requests += 1

    def get_request_rate(self) -> float:
        """
        Calculate current request rate within the sliding window.
        
        Returns:
            float: Requests per second over the current window
        """
        with self.lock:
            current_time = datetime.now()
            duration = (current_time - self.last_reset).total_seconds()
            
            if duration >= self.window_size:
                rate = self.request_count / duration
                self.request_count = 0
                self.last_reset = current_time
                return rate
            elif duration > 0:
                return self.request_count / duration
            return 0.0

    def get_total_requests(self) -> int:
        """
        Get total number of requests processed since initialization.
        
        Returns:
            int: Total request count
        """
        with self.lock:
            return self._total_requests
    
@dataclass
class BatchRequest:
    """Represents a batch of requests for processing"""
    model_name: str
    inputs: List[torch.Tensor]
    batch_size: int
    request_ids: List[str]
    arrival_times: Dict[str, float]  # Map request_id to its arrival time
    batch_arrival_time: float

@ray.remote
class SharedSLOTracker:
    def __init__(self):
        self.queue = RayQueue()
        
    def write_metric(self, model_id: int, latency: float, request_id: str):
        self.queue.put((time.time(), model_id, latency, request_id))

    def get_queue(self):
        return self.queue
        
    def cleanup(self):
        pass

class RequestQueue:
    """Request queue with monitoring capabilities using Ray's Queue"""
    def __init__(self, model_name: str, max_size: int = 100, batch_profile: dict = {}):
        self.model_name = model_name
        self.queue = RayQueue(maxsize=max_size)
        self._logger = logging.getLogger(f"Queue-{model_name}")


        self.model_id = hash(model_name) % 1000

        self.slo_target = models_config[model_name]['SLO']
        self.profile = batch_profile[model_name]
        self.slo_tracker = None  # Will be set by scheduler


        # Simple metrics tracking
        self.window_size = 60  # 1 minute window for rolling metrics
        self.request_timestamps = deque(maxlen=1000)  # Store recent request timestamps
        self.latencies = deque(maxlen=1000)  # Store recent latencies
        self.slo_violations = 0
        self.total_requests = 0
        self.current_window_violations = 0
        self.current_window_requests = 0
        self.window_start_time = time.time()

        self.dropped_requests = 0

        
    def empty(self) -> bool:
        """Check if queue is empty"""
        return self.queue.qsize() == 0
    
    def _update_window_metrics(self, current_time: float):
        """Update rolling window metrics"""
        window_start = current_time - self.window_size
        
        # Remove old requests from current window
        while self.request_timestamps and self.request_timestamps[0] < window_start:
            self.request_timestamps.popleft()
            
        # Update current window metrics
        self.current_window_requests = len(self.request_timestamps)

    def add_request(self, request_id: str, input_tensor: torch.Tensor) -> bool:
        """Add request to queue with monitoring"""
        try:
            if self.queue.full():
                self._logger.warning(f"Queue full for {self.model_name}")
                self.dropped_requests += 1
                return False
            
            current_time = time.time()
            self.queue.put((request_id, input_tensor, current_time))
            self.request_timestamps.append(current_time)
            self._update_window_metrics(current_time)
            self.total_requests += 1
            return True
        except Exception as e:
            self._logger.error(f"Error adding request: {e}")
            return False
    
    def get_batch(self, batch_size: int) -> Optional[BatchRequest]:
        """Get batch of requests with timeout handling"""
        requests = []
        inputs = []
        request_ids = []
        arrival_times = {}
        earliest_arrival = float('inf')
        
        try:
            available = min(batch_size, self.queue.qsize())
            if available == 0:
                return None
                
            '''
            batch = self.queue.get_batch(available, timeout=0)
            for (request_id, input_tensor, arrival_time) in batch:
            # for _ in range(available):
            #     request_id, input_tensor, arrival_time = self.queue.get_nowait()
                requests.append((request_id, input_tensor))
                request_ids.append(request_id)
                inputs.append(input_tensor)
                earliest_arrival = min(earliest_arrival, arrival_time)
                self._pending_count -= 1

            # for _ in range(available):
            #     request_id, input_tensor, arrival_time = self.queue.get_nowait()
            #     requests.append((request_id, input_tensor))
            #     request_ids.append(request_id)
            #     inputs.append(input_tensor)
            #     earliest_arrival = min(earliest_arrival, arrival_time)
            #     self._pending_count -= 1'''
            batch = self.queue.get_nowait_batch(available)
            if not batch:
                return None
            
            for rid, tensor, arrival_time in batch:
                request_ids.append(rid)
                inputs.append(tensor)
                arrival_times[rid] = arrival_time
                earliest_arrival = min(earliest_arrival, arrival_time)
                    
            if inputs:
                return BatchRequest(
                    model_name=self.model_name,
                    inputs=inputs,
                    batch_size=len(inputs),
                    request_ids=request_ids,
                    arrival_times=arrival_times,
                    batch_arrival_time=earliest_arrival
                )

        except Empty:
            pass
        except Exception as e:
            self._logger.error(f"Error creating batch: {e}")
            
        return None
    
    def record_batch_completion(self, batch: BatchRequest, completion_time: float):
        """Record batch completion with enhanced SLO tracking"""
        current_time = time.time()
        latency = (completion_time - batch.batch_arrival_time) * 1000  # Convert to ms

        

        for request_id in batch.request_ids:
            request_latency = (completion_time - batch.arrival_times[request_id]) * 1000
            if self.slo_tracker:
                self.slo_tracker.write_metric.remote(self.model_id, request_latency, request_id)
            self.latencies.append(request_latency)
            
            if latency > self.slo_target:
                self.slo_violations += 1
                self.current_window_violations += 1
                
                self._logger.warning(
                    f"SLO violation - Request ID: {request_id}, "
                    f"Latency: {latency:.2f}ms, Target: {self.slo_target}ms"
                )
                
        self._update_window_metrics(current_time)
    
    def get_stats(self) -> Dict:
        """Get enhanced queue statistics"""
        current_time = time.time()
        self._update_window_metrics(current_time)
        
        latencies = list(self.latencies)
        if not latencies:
            return {
                'total_requests': self.total_requests,
                'dropped_requests': self.dropped_requests,
                'slo_violations': self.slo_violations,
                'queue_size': self.queue.qsize(),
                'window_violation_rate': 0.0,
                'avg_latency': 0,
                'p95_latency': 0,
                'p99_latency': 0
            }
        # Calculate percentiles properly
        sorted_latencies = sorted(latencies)
        p95_idx = int(len(sorted_latencies) * 0.95)
        p99_idx = int(len(sorted_latencies) * 0.99)
        
        window_violation_rate = (
            (self.current_window_violations / self.current_window_requests * 100)
            if self.current_window_requests > 0 else 0
        )
        
        return {
            'total_requests': self.total_requests,
            'dropped_requests': self.dropped_requests,
            'slo_violations': self.slo_violations,
            'queue_size': self.queue.qsize(),
            'window_violation_rate': window_violation_rate,
            'avg_latency': sum(latencies) / len(latencies),
            'p95_latency': sorted_latencies[p95_idx],
            'p99_latency': sorted_latencies[p99_idx],
            'current_window_requests': self.current_window_requests,
            'current_window_violations': self.current_window_violations
        }

@ray.remote(num_gpus=1)
class GPUWorker:
    """ Ray actor for GPU computation"""
    def __init__(self, node_id: str, gpu_id: int, sessions: List[Tuple], 
                 duty_cycle: float, model_registry: Dict):
        self.node_id = node_id
        self.gpu_id = 0
        self.duty_cycle = duty_cycle
        self.sessions = deque(sessions)
        self.models = {}
        self.new_sessions = None
        self.new_duty_cycle = None
        self.lock = Lock()
        self.model_registry = model_registry
        self.device = 'cuda:0'
        self.logger = logging.getLogger(f"Worker-{node_id}")
        # Add diagnostic information
        print(f"Worker {node_id} initialization:")
        print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES')}")
        print(f"Ray GPU IDs: {ray.get_gpu_ids()}")
        print(f"PyTorch GPU count: {torch.cuda.device_count()}")
        print(f"Requested GPU ID: {gpu_id}")
        
        for i in range(torch.cuda.device_count()):
            print(f"GPU {i}: {torch.cuda.get_device_name(i)}")

        print("GPU ID: "+str(self.gpu_id)+"--**--____----++++"+str(gpu_id))
        # Initialize models
        try:
            device = f'cuda:{self.gpu_id}'
            device = 'cuda:0'
            # First check if device is available
            if self.gpu_id >= torch.cuda.device_count():
                raise ValueError(f"GPU {self.gpu_id} not available. Only {torch.cuda.device_count()} GPUs found.")
                
            for session, _ in sessions:
                if session.model_name not in self.models:
                    self.logger.info(f"Loading {session.model_name} on GPU {gpu_id}")
                    model = model_registry[session.model_name]
                    # Move model to CPU first then to specific GPU
                    model = model.cpu()
                    model = model.to(device)
                    model.eval()
                    self.models[session.model_name] = model
                    
        except Exception as e:
            self.logger.error(f"Error initializing models: {e}")
            raise
        
        self.active = True
        self.stats = {
            'processed_batches': 0,
            'total_requests': 0,
            'processing_times': []
        }
    
    def stop(self):
        """Stop the worker gracefully"""
        self.logger.info(f"Stopping worker {self.node_id}")
        self.active = False

    def process_batch(self, batch: BatchRequest, request_queues: Dict[str, RequestQueue]) -> Dict:
        """Process batch with enhanced monitoring"""
        try:
            
            start_time = time.time()

            model = self.models[batch.model_name]
            #inputs = torch.stack(batch.inputs).to(f'cuda:{self.gpu_id}')
            inputs = torch.stack(batch.inputs).cuda()  # Just use cuda() since only one GPU is visible

            #with torch.cuda.device(self.gpu_id):
            with torch.cuda.device(0):  # Always use device 0
                torch.cuda.synchronize()  # Ensure GPU is ready
                
                with torch.no_grad():  # Disable gradient computation
                    outputs = model(inputs)
                
                torch.cuda.synchronize()  # Wait for completion
                
                completion_time = time.time()
                processing_time = (completion_time - start_time) * 1000  # ms
                
                self.stats['processed_batches'] += 1
                self.stats['total_requests'] += len(batch.request_ids)
                self.stats['processing_times'].append(processing_time)

                # Record completion
                request_queues[batch.model_name].record_batch_completion(
                    batch, completion_time
                )
                
                return {
                    'outputs': outputs.cpu(),
                    'request_ids': batch.request_ids,
                    'processing_time': processing_time,
                    'latency': time.time() - batch.batch_arrival_time
                }
        except Exception as e:
            self.logger.error(f"Error processing batch: {e}")
            raise
    
    def _update_schedule(self, new_sessions: List[session], new_duty_cycle: float):
        # print(f"GPUWORKER:_update_schedule: inside update schedule remote call")      
        # print(f"GPUWORKER:_update_schedule: new sessions length {len(new_sessions)}")
        self.new_sessions   = new_sessions
        self.new_duty_cycle = new_duty_cycle

    def _check_for_updates(self, update_queue: RayQueue):
        if update_queue.qsize() == 0:
            return False
        
        while update_queue.qsize() > 1:
            _ = update_queue.get()
        
        latest = update_queue.get()
        self.new_sessions   = latest[0]
        self.new_duty_cycle = latest[1]
        
        if self.new_sessions != None:
            # transition from old schedule to new one
            new_model_list = [s.model_name for s, _ in self.new_sessions]
            old_model_list = [s.model_name for s, _ in self.sessions]

            # first unload all models not present in the new session
            for model_name in old_model_list:
                if model_name not in new_model_list:
                    # unload model
                    self.models[model_name].cpu()
                    del self.models[model_name]
                    torch.cuda.empty_cache()

            # load new models to gpu
            for model_name in new_model_list:
                if model_name not in old_model_list:
                    # load model to gpu
                    model = self.model_registry[model_name]
                    model = model.cpu()
                    model = model.to(self.device)
                    model.eval()
                    self.models[model_name] = model

            self.sessions   = deque(self.new_sessions.copy())
            self.duty_cycle = self.new_duty_cycle

            self.new_sessions   = None
            self.new_duty_cycle = None

            return True

    def execute_schedule(self, request_queues: Dict[str, RequestQueue], update_queue: RayQueue):
        """Execute round-robin schedule with enhanced monitoring"""
        self.logger.info(f"Starting schedule execution on {self.node_id}")
    
        while self.active:
            # print(f"GPU:WORKER:execute_schedule: Starting new dutry cycle")
            try:
                total_time       = self.duty_cycle
                cycle_start_time = time.time()

                if len(self.sessions) == 0:
                    time.sleep(2/100)
                    
                for s, occupancy in self.sessions:
                    # print(f"GPU:WORKER:execute_schedule: looking at session {s.model_name}")
                    # calculate current time slice
                    time_slice         = total_time * occupancy
                    session_start_time = time.time()

                    # Get queue for current model
                    queue = request_queues[s.model_name]

                    # Try to get batch from queue
                    # print(f"calling get batch for {s.model_name}")
                    # print(f"GPU:WORKER:execute_schedule: Getting batch of size {s.batch_size}")
                    batch = queue.get_batch(s.batch_size)
                    if batch:
                        # print(f"GPU:WORKER:execute_schedule: Valid batch found")
                        # Process batch and measure timing
                        result = self.process_batch(batch, request_queues)
                        processing_time = result['processing_time']
                    
                        # Log processing metrics
                        self.logger.info(
                            f"Processed batch of {batch.batch_size} requests for {s.model_name} "
                            f"in {processing_time:.2f}ms"
                        )
                    
                        # Sleep for remaining time if any
                        remaining_time = time_slice - processing_time
                        if remaining_time > 0:
                            time.sleep(remaining_time / 1000)
                    else:
                        # No requests, sleep for time slice
                        time.sleep(time_slice / 1000)

                    # Log execution stats periodically
                    if self.stats['processed_batches'] % 100 == 0:
                        self.logger.info(f"Node {self.node_id} stats: {self.get_stats()}")
                
                # check if worker needs to update node session at the end of the duty cycle
                if not self._check_for_updates(update_queue):
                    # wait for duty cycle to finish
                    current_time    = time.time()
                    remaining_cycle = current_time - (cycle_start_time + (total_time / 1000)) 
                    if remaining_cycle > 0:
                        # print(f"GPU:WORKER:execute_schedule: Going to sleep for {remaining_cycle}")
                        time.sleep(remaining_cycle)

            except Exception as e:
                self.logger.error(f"Error in schedule execution: {e}")
                time.sleep(0.1)
    
    def get_stats(self) -> Dict:
        """Get worker statistics"""
        return {
            'node_id': self.node_id,
            'gpu_id': self.gpu_id,
            'processed_batches': self.stats['processed_batches'],
            'total_requests': self.stats['total_requests'],
            'avg_processing_time': sum(self.stats['processing_times'][-100:]) / 
                                 len(self.stats['processing_times'][-100:])
                                 if self.stats['processing_times'] else 0
        }

class NexusScheduler:
    """
        This class implements the squishy bin packing algorithm described
        in section 6.1 of nexus paper

        batching profile dictionary needs to be structured as follows:
        key = batch size: { key = column name in csv(latency, memory usage etc): value}
    """
    def __init__(self, batching_profile: Dict[str, Dict[int, Dict[str, float]]], 
                 monitoring_interval: float = 5.0,
                 rate_change_threshold: float = 0.05):
        self.batching_profile = batching_profile
        self.nexus_instance   = nexus(batching_profile)
        self.sessions: Dict[str, session] = {}
        self.nodes: List[node] = []
        # self.nodes: Dict[str, List[node]] = {}
        self.request_trackers: Dict[str, RequestTracker] = {}
        
        self.monitoring_interval = monitoring_interval
        self.rate_change_threshold = rate_change_threshold
        self.lock = Lock()
        self.schedule_changes = Queue()
        
        self.monitoring_thread: Optional[Thread] = None
        self._stop_monitoring = False
        self.logger = logging.getLogger("NexusScheduler")

        # Initialize SLO tracker
        self.slo_tracker = SharedSLOTracker.options(name="slo_tracker", namespace='SLOMonitoring').remote()
        print(f"Created tracker in namespace: {ray.get_runtime_context().namespace}")


        # Initialize request queues
        self.request_queues = {}
        self.update_queues  = []
        self._init_queues(2000)
        
        # Initialize workers
        self.model_registry = model_registry
        self.workers: List[GPUWorker] = []
        self.futures = []
        self._init_workers()
        self._start_workers()



        # Initialize metrics display
        #self.metrics_display = MetricsDisplay(update_interval=5.0)
        #self.metrics_display.start(self.request_queues)

        # Add metrics tracking
        self.metrics: Dict[str, Dict] = {
            'schedule_updates': 0,
            'total_requests': {},
            'rate_changes': {},
            'node_changes': {}
        }
    
    def _init_queues(self, max_queue_size: int):
        """Initialize request queues for all models"""
        self.slo_queue = ray.get(self.slo_tracker.get_queue.remote())

        for model in models_config.keys():
            queue = RequestQueue(
                model_name=model,
                max_size=max_queue_size,
                batch_profile=self.batching_profile
            )

            queue.slo_tracker = self.slo_tracker 
            self.request_queues[model] = queue
            self.request_trackers[model] = RequestTracker(1)
        # for schedule in self.node_schedules:
        #     for session, _ in schedule['sessions']:
        #         if session.model_name not in self.request_queues:
        #             self.request_queues[session.model_name] = RequestQueue(
        #                 model_name=session.model_name,
        #                 max_size=max_queue_size
        #             )

    def __del__(self):
        if hasattr(self, 'slo_tracker'):
            self.slo_tracker.cleanup()

    def _init_workers(self):
        """Initialize GPU workers"""
        available_gpus = torch.cuda.device_count()
        required_gpus = len(self.nodes)
        if required_gpus > available_gpus:
            raise RuntimeError(f"Schedule requires {required_gpus} GPUs but only {available_gpus} available")

        # initialise two ndoes
        for i in range(2):
            try:
                worker = GPUWorker.remote(
                    node_id='A6000_' + str(i),
                    gpu_id=0,
                    sessions=[],
                    duty_cycle=1,
                    model_registry=self.model_registry
                )
                self.workers.append(worker)

                self.update_queues.append(RayQueue(maxsize=0))
            except Exception as e:
                self.logger.error(f"Error initializing worker: {e}")
                    

    def _start_workers(self):
        """Start schedule execution with monitoring"""
        self.start_time = time.time()
        self.logger.info("Starting schedule execution")

        try:
            # Create ray actors and start execution
            ind = 0
            for worker in self.workers:
                self.futures.append(worker.execute_schedule.remote(self.request_queues, self.update_queues[ind]))
                ind+=1
        
            # Start monitoring thread
            # self.monitoring_thread = threading.Thread(target=self._monitor_system)
            # self.monitoring_thread.daemon = True
            # self.monitoring_thread.start()
        
        except Exception as e:
            self.logger.error(f"Error starting execution: {e}")
            raise

    def start_monitoring(self) -> None:
        """Start the monitoring thread"""
        if self.monitoring_thread is not None:
            return
        
        self._stop_monitoring = False
        self.monitoring_thread = Thread(target=self._monitor_request_rates, daemon=True)
        self.monitoring_thread.start()
        self.logger.info("Request rate monitoring started")
    
    def stop_monitoring(self) -> None:
        """Stop the monitoring thread"""
        self._stop_monitoring = True
        if self.monitoring_thread:
            self.monitoring_thread.join()
            self.monitoring_thread = None
        self.logger.info("Request rate monitoring stopped")

    def submit_request(self, model_name: str, request_id: str, 
                      input_tensor: torch.Tensor) -> bool:
        """Submit request with error handling"""
        try:
            if model_name not in self.request_queues:
                self.logger.error(f"No queue found for model {model_name}")
                return False
            
            success = self.request_queues[model_name].add_request(
                request_id, input_tensor
            )

            self.request_trackers[model_name].record_request()
            
            return success
        except Exception as e:
            self.logger.error(f"Error submitting request: {e}")
            return False

    # def record_request(self, model_name: str) -> None:
    #     """Record an incoming request for a model"""
    #     if model_name not in self.request_trackers:
    #         with self.lock:
    #             if model_name not in self.request_trackers:
    #                 self.request_trackers[model_name] = RequestTracker()
    #                 self.metrics['total_requests'][model_name] = 0
    #     self.request_trackers[model_name].record_request()
    #     self.metrics['total_requests'][model_name] += 1
    
    def _monitor_request_rates(self) -> None:
        """Background monitoring loop"""
        while not self._stop_monitoring:
            try:
                self._check_and_update_schedules()
                time.sleep(self.monitoring_interval)
            except Exception as e:
                self.logger.error(f"Monitoring error: {e}")
                self.logger.error(f"Stack trace: {traceback.format_exc()}")
    
    def _check_and_update_schedules(self):
        """Check request rates and update schedules if needed"""
        requires_update = False
        update_info     = {}
        
        with self.lock:
            for model_name, tracker in self.request_trackers.items():
                current_rate = tracker.get_request_rate()
                
                if model_name not in self.sessions:
                    requires_update = True
                    update_info[model_name] = current_rate
                    continue

                previous_rate = self.sessions[model_name].request_rate
                rate_diff = abs(current_rate - previous_rate)

                # Check if rate change exceeds threshold
                if (rate_diff / previous_rate) > self.rate_change_threshold:
                    self.logger.info(f"Rate change detected for {model_name}: {current_rate:.2f} req/s")
                    
                    requires_update = True
                    update_info[model_name] = current_rate 
                    # self._update_schedule(model_name, current_rate)
                    
                    # Update metrics
                    if model_name not in self.metrics['rate_changes']:
                        self.metrics['rate_changes'][model_name] = []
                    self.metrics['rate_changes'][model_name].append({
                        'timestamp': datetime.now(),
                        'old_rate': previous_rate,
                        'new_rate': current_rate
                    }) 

        if requires_update:
            self._update_schedule(update_info)

    def get_transfers(self, old_nodes: List[node], new_nodes: List[node]):
        transfers = 0

        for old_node, new_node in zip(old_nodes, new_nodes):
            new_node_models = [s.model_name for s, _ in new_node.node_sessions]
            old_node_models = [s.model_name for s, _ in old_node.node_sessions]
        
            for model in new_node_models:
                if model not in old_node_models:
                    transfers += 1
        
        return transfers

    def _update_schedule(self, update_info: dict):
        new_sessions = []
        # update request rates of all old sessions
        for model, old_session in self.sessions.items():
            new_session = copy.deepcopy(old_session)
            if model in update_info:
                new_session.request_rate = update_info[model]
            if new_session.request_rate > 0:
                new_sessions.append(new_session)
        
        # also add sessions that have been created for new models
        for model, request_rate in update_info.items():
            if model not in self.sessions:
                new_session = session(model, models_config[model]['SLO'] / SLO_hack, request_rate)
                if new_session.request_rate > 0:
                    new_sessions.append(new_session)

        old_nodes = self.nodes
        new_nodes = self.nexus_instance.squishyBinPacking(new_sessions)

        # Find a way to update from old arrangement to new arrangement such that
        # the number of model transfers across GPUs is minimized
        l = len(old_nodes)
        n = len(new_nodes)
        final_nodes = []
        if l <= n:
            numbers = range(1, n + 1)
            arrangments = list(itertools.permutations(numbers, l))

            best_arrangment = None
            min_transfers   = None
            for arrangement in arrangments:
                current_transfers = self.get_transfers(old_nodes, [new_nodes[i-1] for i in arrangement])
                if min_transfers is None or min_transfers > current_transfers:
                    best_arrangment = arrangement
                    min_transfers   = current_transfers
            
            final_nodes = [new_nodes[i-1] for i in best_arrangment]
            for i in range(1, n+1):
                if i not in best_arrangment:
                    final_nodes.append(new_nodes[i-1])
        else:
            numbers = range(1, n + 1)
            arrangments = list(itertools.permutations(numbers, l))

            best_arrangment = None
            min_transfers   = None
            for arrangement in arrangments:
                current_transfers = self.get_transfers(old_nodes[:n], [new_nodes[i-1] for i in arrangement])
                if min_transfers is None or min_transfers > current_transfers:
                    best_arrangment = arrangement
                    min_transfers   = current_transfers

            final_nodes = [new_nodes[i-1] for i in best_arrangment]

        self._update_workers(final_nodes)

        for n in final_nodes:
            n.print_node_pretty()

    def _update_workers(self, new_nodes: List[node]):
        # get workers and set new nodes for them
        # they should load and unload models at the end of duty cycle
        # and then carry on executing the new node

        l = len(self.workers)
        n = len(new_nodes)
        update_refs = []
        for i in range(min(l, n)):
            # print(f"NEXUSSCHEDULER:_update_worker: calling _update_schedule on worker {i}")
            # print(f"NEXUSSCHEDULER:_update_worker: {new_nodes[i].print_node_pretty()}")
            # print(f"NEXUSSCHEDULER:_update_worker: {new_nodes[i].duty_cycle}")
            self.update_queues[i].put((new_nodes[i].node_sessions, new_nodes[i].duty_cycle, time.time()))
            # update_refs.append(self.workers[i]._update_schedule.remote(new_nodes[i].node_sessions, new_nodes[i].duty_cycle))

        if l > n:
            # stop all worker from n:l-1
            for i in range(n, l):
                # print(f"NEXUSSCHEDULER:_update_worker: calling _update_schedule on worker {i} to STOP")
                self.update_queues[i].put(([], 1, time.time()))
                # update_refs.append(self.workers[i]._update_schedule.remote([], 1))

        if n < l:
            # launch new worker node
            pass
        


class MetricsDisplay:
    """Real-time display of SLO metrics for all model queues"""
    def __init__(self, update_interval: float = 1.0):
        self.update_interval = update_interval
        self.stop_display = False
        self.display_thread = None
        self.clear_status = False
        self.lock = Lock()
        
    def start(self, request_queues: Dict[str, RequestQueue]):
        """Start metrics display thread"""
        self.stop_display = False
        self.display_thread = Thread(
            target=self._display_loop, 
            args=(request_queues,),
            daemon=True
        )
        self.display_thread.start()
        
    def stop(self):
        """Stop metrics display"""
        self.stop_display = True
        if self.display_thread:
            self.display_thread.join()
            
    def _display_loop(self, request_queues: Dict[str, RequestQueue]):
        """Main display loop"""
        while not self.stop_display:
            # Clear screen
            if self.clear_status:
                os.system('clear' if os.name == 'posix' else 'cls')
            
            print("\n=== Real-time SLO Metrics ===")
            print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 50)
            
            # Display metrics for each model
            for model_name, queue in request_queues.items():
                stats = queue.get_stats()
                slo_target = queue.slo_target
                
                print(f"\nModel: {model_name}")
                print("-" * 30)
                print(f"SLO Target: {slo_target}ms")
                print(f"Queue Size: {stats['queue_size']}")
                print(f"Total Requests: {stats['total_requests']}")
                print(f"Dropped Requests: {stats['dropped_requests']}")
                print(f"SLO Violations: {stats['slo_violations']}")
                
                # Calculate SLO compliance percentage
                compliance = 100.0  # Default to 100% if no requests
                if stats['total_requests'] > 0:
                    compliance = ((stats['total_requests'] - stats['slo_violations']) / 
                                stats['total_requests'] * 100)
                print(f"SLO Compliance: {compliance:.2f}%")
                
                # Display latency metrics
                print(f"Average Latency: {stats['avg_latency']:.2f}ms")
                print(f"P95 Latency: {stats['p95_latency']:.2f}ms")
                
                # Visual indicators
                queue_status = "🟢" if stats['queue_size'] < queue.queue.maxsize * 0.8 else "🔴"
                slo_status = "🟢" if compliance >= 95 else "🔴" if compliance < 90 else "🟡"
                print(f"Queue Status: {queue_status}  SLO Status: {slo_status}")
            
            print("\n" + "=" * 50)
            print("🟢 Good  🟡 Warning  🔴 Critical")
            print(f"Display updating every {self.update_interval} seconds...")
            
            time.sleep(self.update_interval)
def main():
    """
    Main function for running the dynamic scheduling system.
    
    Configuration:
    - Sets up logging and test results directory
    - Loads batch profiles for models (vit, resnet, shufflenet, efficientnet)
    - Initializes scheduler with monitoring interval=5.0s, rate threshold=0.05
    - Configures initial model deployments with SLOs and request rates
    
    Operations:
    - Performs initial scheduling using squishyBinPacking
    - Starts continuous monitoring of request rates 
    - Simulates varying workload patterns for each model
    - Logs metrics, node states and schedule changes every 10s
    
    Monitoring continues until keyboard interrupt (Ctrl+C), then performs
    cleanup and saves final metrics.
    
    Directory structure:
    profiling_dir: Contains model batch profiles (.csv files)
    logger.test_dir: Stores test results and metrics
    """

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Set up result logger
    logger = TestResultLogger()
    logging.info(f"Test results will be stored in: {logger.test_dir}")

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
        
    # Create scheduler instance
    scheduler = NexusScheduler(
        batching_profile=batching_profile,
        monitoring_interval=5.0,
        rate_change_threshold=0.05
    )

    # Register initial models

if __name__ == '__main__':
    main()

