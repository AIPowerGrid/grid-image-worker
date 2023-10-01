import asyncio
import base64
import multiprocessing
import time
from asyncio import Lock as Lock_Asyncio
from collections import deque
from collections.abc import Mapping
from io import BytesIO
from multiprocessing.connection import PipeConnection
from multiprocessing.context import BaseContext
from multiprocessing.synchronize import Lock as Lock_MultiProcessing
from multiprocessing.synchronize import Semaphore

import aiohttp
import PIL
import PIL.Image
import psutil
import requests
import torch
from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError
from horde_model_reference.meta_consts import MODEL_REFERENCE_CATEGORY, STABLE_DIFFUSION_BASELINE_CATEGORY
from horde_model_reference.model_reference_manager import ModelReferenceManager
from horde_model_reference.model_reference_records import StableDiffusion_ModelReference
from horde_sdk import RequestErrorResponse
from horde_sdk.ai_horde_api import GENERATION_STATE
from horde_sdk.ai_horde_api.ai_horde_clients import AIHordeAPIAsyncClientSession, AIHordeAPIAsyncSimpleClient
from horde_sdk.ai_horde_api.apimodels import (
    FindUserRequest,
    FindUserResponse,
    ImageGenerateJobPopRequest,
    ImageGenerateJobPopResponse,
    JobSubmitResponse,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, RootModel

from horde_worker_regen.bridge_data.data_model import reGenBridgeData
from horde_worker_regen.process_management._aliased_types import ProcessQueue
from horde_worker_regen.process_management.inference_process import HordeProcessKind
from horde_worker_regen.process_management.messages import (
    HordeControlFlag,
    HordeControlMessage,
    HordeControlModelMessage,
    HordeInferenceControlMessage,
    HordeInferenceResultMessage,
    HordeModelStateChangeMessage,
    HordePreloadInferenceModelMessage,
    HordeProcessMemoryMessage,
    HordeProcessMessage,
    HordeProcessState,
    HordeProcessStateChangeMessage,
    HordeSafetyControlMessage,
    HordeSafetyResultMessage,
    ModelInfo,
    ModelLoadState,
)
from horde_worker_regen.process_management.worker_entry_points import start_inference_process, start_safety_process


class HordeProcessInfo:
    mp_process: multiprocessing.Process
    pipe_connection: PipeConnection
    process_id: int
    process_type: HordeProcessKind
    last_process_state: HordeProcessState
    loaded_horde_model_name: str | None = None

    ram_usage_bytes: int = 0
    vram_usage_bytes: int = 0
    total_vram_bytes: int = 0

    # TODO: VRAM usage

    def __init__(
        self,
        mp_process: multiprocessing.Process,
        pipe_connection: PipeConnection,
        process_id: int,
        process_type: HordeProcessKind,
        last_process_state: HordeProcessState,
    ) -> None:
        self.mp_process = mp_process
        self.pipe_connection = pipe_connection
        self.process_id = process_id
        self.process_type = process_type
        self.last_process_state = last_process_state

    def is_process_busy(self) -> bool:
        return not self.last_process_state.can_accept_job()

    def __repr__(self) -> str:
        return str(
            f"HordeProcessInfo(process_id={self.process_id}, last_process_state={self.last_process_state}, "
            f"loaded_horde_model_name={self.loaded_horde_model_name})",
        )

    def can_accept_job(self) -> bool:
        return self.last_process_state.can_accept_job()


class HordeModelMap(RootModel[dict[str, ModelInfo]]):
    def update_entry(
        self,
        horde_model_name: str,
        *,
        load_state: ModelLoadState | None = None,
        process_id: int | None = None,
    ) -> None:
        if horde_model_name not in self.root:
            if process_id is None:
                raise ValueError("process_id must be provided when adding a new model to the map")
            if load_state is None:
                raise ValueError("model_load_state must be provided when adding a new model to the map")

            self.root[horde_model_name] = ModelInfo(
                horde_model_name=horde_model_name,
                horde_model_load_state=load_state,
                process_id=process_id,
            )

        if load_state is not None:
            self.root[horde_model_name].horde_model_load_state = load_state

        if process_id is not None:
            self.root[horde_model_name].process_id = process_id

    def is_model_loaded(self, horde_model_name: str) -> bool:
        if horde_model_name not in self.root:
            return False
        return self.root[horde_model_name].horde_model_load_state.is_loaded()

    def is_model_loading(self, horde_model_name: str) -> bool:
        if horde_model_name not in self.root:
            return False
        return self.root[horde_model_name].horde_model_load_state == ModelLoadState.LOADING


class ProcessMap(dict[int, HordeProcessInfo]):
    """A mapping of process IDs to HordeProcessInfo objects. Contains some helper methods."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def update_entry(
        self,
        process_id: int,
        *,
        last_process_state: HordeProcessState | None = None,
        loaded_horde_model_name: str | None = None,
        ram_usage_bytes: int | None = None,
        vram_usage_bytes: int | None = None,
        total_vram_bytes: int | None = None,
    ) -> None:
        if last_process_state is not None:
            self[process_id].last_process_state = last_process_state

        if loaded_horde_model_name is not None:
            self[process_id].loaded_horde_model_name = loaded_horde_model_name

        if ram_usage_bytes is not None:
            self[process_id].ram_usage_bytes = ram_usage_bytes

        if vram_usage_bytes is not None:
            self[process_id].vram_usage_bytes = vram_usage_bytes

        if total_vram_bytes is not None:
            self[process_id].total_vram_bytes = total_vram_bytes

    def num_inference_processes(self) -> int:
        count = 0
        for p in self.values():
            if p.process_type == HordeProcessKind.INFERENCE:
                count += 1
        return count

    def num_available_inference_processes(self) -> int:
        count = 0
        for p in self.values():
            if p.process_type == HordeProcessKind.INFERENCE and not p.is_process_busy():
                count += 1
        return count

    def get_first_available_inference_process(self) -> HordeProcessInfo | None:
        for p in self.values():
            if p.process_type == HordeProcessKind.INFERENCE and p.can_accept_job():
                return p
        return None

    def get_safety_process(self) -> HordeProcessInfo | None:
        for p in self.values():
            if p.process_type == HordeProcessKind.SAFETY:
                return p
        return None

    def num_safety_processes(self) -> int:
        count = 0
        for p in self.values():
            if p.process_type == HordeProcessKind.SAFETY:
                count += 1
        return count

    def get_first_available_safety_process(self) -> HordeProcessInfo | None:
        for p in self.values():
            if p.process_type == HordeProcessKind.SAFETY and p.can_accept_job():
                return p
        return None

    def get_process_by_horde_model_name(self, horde_model_name: str) -> HordeProcessInfo | None:
        for p in self.values():
            if p.loaded_horde_model_name == horde_model_name:
                return p
        return None


class TorchDeviceInfo(BaseModel):
    device_name: str
    device_index: int
    total_memory: int


class TorchDeviceMap(RootModel[dict[int, TorchDeviceInfo]]):
    pass


class CompletedJobInfo(BaseModel):
    job_info: ImageGenerateJobPopResponse
    job_result_images_base64: list[str] | None = None
    state: GENERATION_STATE
    censored: bool | None = None

    @property
    def is_job_checked_for_safety(self) -> bool:
        return self.censored is not None


class HordeWorkerProcessManager:
    """Manages and controls processes to act as a horde worker."""

    bridge_data: reGenBridgeData
    """The bridge data for this worker."""

    max_inference_processes: int
    """The maximum number of inference processes that can be active. This is not the number of jobs that
    can run at once. Use `max_concurrent_inference_processes` to control that behavior."""
    max_concurrent_inference_processes: int
    """The maximum number of inference processes that can run jobs concurrently."""
    max_safety_processes: int
    """The maximum number of safety processes that can run at once."""
    max_download_processes: int
    """The maximum number of download processes that can run at once."""

    total_ram_bytes: int
    """The total amount of RAM on the system."""
    target_ram_overhead_bytes: int
    """The target amount of RAM to keep free."""

    target_vram_overhead_bytes_map: Mapping[int, int] | None = None

    @property
    def max_queue_size(self) -> int:
        return self.bridge_data.queue_size

    @property
    def current_queue_size(self) -> int:
        return len(self.job_deque)

    @property
    def target_ram_bytes_used(self) -> int:
        return self.total_ram_bytes - self.target_ram_overhead_bytes

    def get_process_total_ram_usage(self) -> int:
        total = 0
        for process_info in self._process_map.values():
            total += process_info.ram_usage_bytes
        return total

    jobs_in_progress: list[ImageGenerateJobPopResponse]
    """A list of jobs that are currently in progress."""

    jobs_pending_safety_check: list[CompletedJobInfo]
    _jobs_safety_check_lock: Lock_Asyncio

    jobs_being_safety_checked: list[CompletedJobInfo]

    completed_jobs: list[CompletedJobInfo]
    """A list of 3 tuples containing the job, the state, and whether or not the job was censored."""

    _completed_jobs_lock: Lock_Asyncio

    _aiohttp_session: aiohttp.ClientSession

    stable_diffusion_reference: StableDiffusion_ModelReference | None
    horde_client: AIHordeAPIAsyncSimpleClient
    horde_client_session: AIHordeAPIAsyncClientSession

    user_info: FindUserResponse | None = None
    """The user info for the user that this worker is logged in as."""
    _last_user_info_fetch_time: float = 0
    """The time at which the user info was last fetched."""
    _user_info_fetch_interval: float = 10
    """The number of seconds between each fetch of the user info."""

    _process_map: ProcessMap
    """A mapping (dict) of process IDs to HordeProcessInfo objects. Contains some helper methods."""
    _horde_model_map: HordeModelMap
    """A mapping (dict) of horde model names to ModelInfo objects. Contains some helper methods."""
    _device_map: TorchDeviceMap
    """A mapping (dict) of device IDs to TorchDeviceInfo objects. Contains some helper methods."""

    _loop_interval: float = 0.1
    """The number of seconds to wait between each loop of the main process (inter process management) loop."""
    _api_call_loop_interval = 0.1
    """The number of seconds to wait between each loop of the main API call loop."""

    _api_get_user_info_interval = 5
    """The number of seconds to wait between each fetch of the user info."""

    _last_get_user_info_time: float = 0
    """The time at which the user info was last fetched."""

    @property
    def num_total_processes(self) -> int:
        return self.max_inference_processes + self.max_safety_processes + self.max_download_processes

    _process_message_queue: ProcessQueue
    """A queue of messages sent from child processes."""

    job_deque: deque[ImageGenerateJobPopResponse]
    """A deque of jobs that are waiting to be processed."""
    _job_deque_lock: Lock_Asyncio

    _inference_semaphore: Semaphore
    """A semaphore that limits the number of inference processes that can run at once."""
    _disk_lock: Lock_MultiProcessing

    def __init__(
        self,
        *,
        ctx: BaseContext,
        bridge_data: reGenBridgeData,
        target_ram_overhead_bytes: int = 2 * 1024 * 1024 * 1024,
        target_vram_overhead_bytes_map: Mapping[int, int] | None = None,  # FIXME
        max_inference_processes: int = 3,
        max_safety_processes: int = 1,
        max_download_processes: int = 1,
        max_concurrent_inference_processes: int = 1,
    ) -> None:
        self.bridge_data = bridge_data

        self._process_map = ProcessMap({})
        self._horde_model_map = HordeModelMap(root={})

        self.max_inference_processes = max_inference_processes
        self.max_safety_processes = max_safety_processes
        self.max_download_processes = max_download_processes

        self.max_concurrent_inference_processes = max_concurrent_inference_processes

        self._inference_semaphore = Semaphore(max_concurrent_inference_processes, ctx=ctx)
        self._disk_lock = Lock_MultiProcessing(ctx=ctx)

        self.completed_jobs = []
        self._completed_jobs_lock = Lock_Asyncio()

        self.jobs_pending_safety_check = []
        self.jobs_being_safety_checked = []

        self._jobs_safety_check_lock = Lock_Asyncio()

        self.target_vram_overhead_bytes_map = target_vram_overhead_bytes_map

        self.total_ram_bytes = psutil.virtual_memory().total
        self.target_ram_overhead_bytes = target_ram_overhead_bytes
        # self.target_ram_overhead_bytes = int(self.total_ram_bytes / 2) # FIXME

        if self.target_ram_overhead_bytes > self.total_ram_bytes:
            raise ValueError(
                f"target_ram_overhead_bytes ({self.target_ram_overhead_bytes}) is greater than "
                "total_ram_bytes ({self.total_ram_bytes})",
            )

        logger.debug(f"Total RAM: {self.total_ram_bytes / 1024 / 1024 / 1024} GB")
        logger.debug(f"Target RAM overhead: {self.target_ram_overhead_bytes / 1024 / 1024 / 1024} GB")

        # Get the total memory of each GPU
        self._device_map = TorchDeviceMap(root={})
        for i in range(torch.cuda.device_count()):
            device = torch.cuda.get_device_properties(i)
            self._device_map.root[i] = TorchDeviceInfo(
                device_name=device.name,
                device_index=i,
                total_memory=device.total_memory,
            )

        self.jobs_in_progress = []

        self.job_deque = deque()
        self._job_deque_lock = Lock_Asyncio()

        self._process_message_queue = multiprocessing.Queue()

        # The parent process already downloaded and converted the model references

        self.stable_diffusion_reference = None

        while self.stable_diffusion_reference is None:
            try:
                horde_model_reference_manager = ModelReferenceManager(
                    download_and_convert_legacy_dbs=False,
                    override_existing=False,
                )
                all_refs = horde_model_reference_manager.get_all_model_references(False)
                _sd_ref = all_refs[MODEL_REFERENCE_CATEGORY.stable_diffusion]

                if not isinstance(_sd_ref, StableDiffusion_ModelReference):
                    raise ValueError("Expected StableDiffusion_ModelReference")

                self.stable_diffusion_reference = _sd_ref
            except Exception as e:
                logger.error(e)
                time.sleep(5)

    def is_time_for_shutdown(self) -> bool:
        if len(self.jobs_in_progress) > 0:
            return False

        if len(self.job_deque) > 0:
            return False

        any_process_alive = False

        for process_info in self._process_map.values():
            if process_info.is_process_busy():
                return False

            if process_info.last_process_state != HordeProcessState.PROCESS_ENDED:
                any_process_alive = True
                continue

        return not any_process_alive

    def is_free_inference_process_available(self) -> bool:
        return self._process_map.num_available_inference_processes() > 0

    def get_expected_ram_usage(self, horde_model_name: str) -> int:
        if self.stable_diffusion_reference is None:
            raise ValueError("stable_diffusion_reference is None")

        horde_model_record = self.stable_diffusion_reference.root[horde_model_name]

        if horde_model_record.baseline == STABLE_DIFFUSION_BASELINE_CATEGORY.stable_diffusion_1:
            return int(3 * 1024 * 1024 * 1024)
        if horde_model_record.baseline == STABLE_DIFFUSION_BASELINE_CATEGORY.stable_diffusion_2_512:
            return 4 * 1024 * 1024 * 1024
        if horde_model_record.baseline == STABLE_DIFFUSION_BASELINE_CATEGORY.stable_diffusion_2_768:
            return 5 * 1024 * 1024 * 1024
        if horde_model_record.baseline == STABLE_DIFFUSION_BASELINE_CATEGORY.stable_diffusion_xl:
            return int(5.75 * 1024 * 1024 * 1024)

        raise ValueError(f"Model {horde_model_name} has an unknown baseline {horde_model_record.baseline}")

    def start_safety_processes(self) -> None:
        """Start all the safety processes configured to be used. This can be used after a configuration
        change to get just the newly configured processes running."""

        num_processes_to_start = self.max_safety_processes - self._process_map.num_safety_processes()

        # If the number of processes to start is less than 0, log a critical error and raise a ValueError
        if num_processes_to_start < 0:
            logger.critical(
                f"There are already {self._process_map.num_safety_processes()} safety processes running, but "
                f"max_safety_processes is set to {self.max_safety_processes}",
            )
            raise ValueError("num_processes_to_start cannot be less than 0")

        # Start the required number of processes

        for _ in range(num_processes_to_start):
            # Create a two-way communication pipe for the parent and child processes
            pid = len(self._process_map)
            pipe_connection, child_pipe_connection = multiprocessing.Pipe(duplex=True)

            # Create a new process that will run the start_safety_process function
            process = multiprocessing.Process(
                target=start_safety_process,
                args=(
                    pid,
                    self._process_message_queue,
                    child_pipe_connection,
                    self._disk_lock,
                ),
            )

            process.start()

            # Add the process to the process map
            self._process_map[pid] = HordeProcessInfo(
                mp_process=process,
                pipe_connection=pipe_connection,
                process_id=pid,
                process_type=HordeProcessKind.SAFETY,
                last_process_state=HordeProcessState.PROCESS_STARTING,
            )

            logger.info(f"Started safety process (id: {pid})")

    def start_inference_processes(self) -> None:
        """Start all the inference processes configured to be used. This can be used after a configuration
        change to get just the newly configured processes running."""

        num_processes_to_start = self.max_inference_processes - self._process_map.num_inference_processes()

        # If the number of processes to start is less than 0, log a critical error and raise a ValueError
        if num_processes_to_start < 0:
            logger.critical(
                f"There are already {self._process_map.num_inference_processes()} inference processes running, but "
                f"max_inference_processes is set to {self.max_inference_processes}",
            )
            raise ValueError("num_processes_to_start cannot be less than 0")

        # Start the required number of processes
        for _ in range(num_processes_to_start):
            # Create a two-way communication pipe for the parent and child processes
            pid = len(self._process_map)
            pipe_connection, child_pipe_connection = multiprocessing.Pipe(duplex=True)

            # Create a new process that will run the start_inference_process function
            process = multiprocessing.Process(
                target=start_inference_process,
                args=(
                    pid,
                    self._process_message_queue,
                    child_pipe_connection,
                    self._inference_semaphore,
                    self._disk_lock,
                ),
            )

            process.start()

            # Add the process to the process map
            self._process_map[pid] = HordeProcessInfo(
                mp_process=process,
                pipe_connection=pipe_connection,
                process_id=pid,
                process_type=HordeProcessKind.INFERENCE,
                last_process_state=HordeProcessState.PROCESS_STARTING,
            )

            logger.info(f"Started inference process (id: {pid})")

    def end_inference_processes(self) -> None:
        """End any inference processes above the configured limit, or all of them if shutting down."""

        if self.is_time_for_shutdown():
            num_processes_to_end = self._process_map.num_inference_processes()
        else:
            num_processes_to_end = self._process_map.num_inference_processes() - self.max_inference_processes

        # If the number of processes to end is less than 0, log a critical error and raise a ValueError
        if num_processes_to_end < 0:
            logger.critical(
                f"There are already {self._process_map.num_inference_processes()} inference processes running, but "
                f"max_inference_processes is set to {self.max_inference_processes}",
            )
            raise ValueError("num_processes_to_end cannot be less than 0")

        # End the required number of processes
        for _ in range(num_processes_to_end):
            # Get the process to end
            process_info = self._process_map.get_first_available_inference_process()

            if process_info is None:
                logger.critical(
                    f"Expected to find {num_processes_to_end} inference processes to end, but found none",
                )
                raise ValueError("Expected to find a process to end, but found none")

            # Send the process a message to end
            process_info.pipe_connection.send(HordeControlMessage(control_flag=HordeControlFlag.END_PROCESS))

            # Update the process map
            self._process_map.update_entry(process_id=process_info.process_id)

            logger.info(f"Ended inference process {process_info.process_id}")

    total_num_completed_jobs: int = 0

    def receive_and_handle_process_messages(self) -> None:
        """Receive and handle any messages from the child processes."""
        while not self._process_message_queue.empty():
            message: HordeProcessMessage = self._process_message_queue.get()

            logger.debug(
                f"Received {type(message).__name__}: "
                f"{message.model_dump(exclude={'job_result_images_base64', 'replacement_image_base64'})}",
            )

            if not isinstance(message, HordeProcessMessage):
                raise ValueError(f"Received a message that is not a HordeProcessMessage: {message}")

            if message.process_id not in self._process_map:
                raise ValueError(f"Received a message from an unknown process: {message}")

            if isinstance(message, HordeProcessStateChangeMessage):
                self._process_map.update_entry(
                    process_id=message.process_id,
                    last_process_state=message.process_state,
                )

                logger.debug(f"Process {message.process_id} changed state to {message.process_state}")
                if message.process_state == HordeProcessState.INFERENCE_STARTING:
                    logger.info(f"Process {message.process_id} is starting inference on model {message.info}")

            if isinstance(message, HordeModelStateChangeMessage):
                self._horde_model_map.update_entry(
                    horde_model_name=message.horde_model_name,
                    load_state=message.horde_model_state,
                    process_id=message.process_id,
                )

                if (
                    message.horde_model_state == ModelLoadState.LOADED_IN_VRAM
                    or message.horde_model_state == ModelLoadState.LOADED_IN_RAM
                ):
                    if (
                        message.process_id in self._process_map
                        and message.horde_model_state != self._process_map[message.process_id].loaded_horde_model_name
                    ):
                        logger.info(f"Process {message.process_id} loaded model {message.horde_model_name}")

                    self._process_map.update_entry(
                        process_id=message.process_id,
                        loaded_horde_model_name=message.horde_model_name,
                    )

                elif message.horde_model_state == ModelLoadState.ON_DISK:
                    self._process_map.update_entry(
                        process_id=message.process_id,
                        loaded_horde_model_name=None,
                    )
                    # FIXME this message is wrong for download processes
                    logger.info(f"Process {message.process_id} unloaded model {message.horde_model_name}")

            if isinstance(message, HordeProcessMemoryMessage):
                self._process_map.update_entry(
                    process_id=message.process_id,
                    ram_usage_bytes=message.ram_usage_bytes,
                    vram_usage_bytes=message.vram_usage_bytes,
                    total_vram_bytes=message.vram_total_bytes,
                )

            if isinstance(message, HordeInferenceResultMessage):
                if message.job_result_images_base64 is None:
                    logger.error(f"Received an inference result message with a None job_result: {message}")
                    continue

                _num_jobs_in_progress = len(self.jobs_in_progress)
                # Remove the job from the jobs in progress by matching the job ID (.id_)

                self.jobs_in_progress = [job for job in self.jobs_in_progress if job.id_ != message.job_info.id_]

                if len(self.jobs_in_progress) != _num_jobs_in_progress - 1:
                    logger.warning(
                        "Expected to remove 1 job from the jobs in progress, but removed "
                        f"{len(self.jobs_in_progress) - _num_jobs_in_progress} jobs",
                    )
                    logger.debug(f"Jobs in progress: {self.jobs_in_progress}")

                self.job_deque.popleft()
                self.total_num_completed_jobs += 1
                logger.info(f"Inference finished for job {message.job_info.id_}")

                self.jobs_pending_safety_check.append(
                    CompletedJobInfo(
                        job_info=message.job_info,
                        job_result_images_base64=message.job_result_images_base64,
                        state=message.state,
                    ),
                )
            elif isinstance(message, HordeSafetyResultMessage):
                completed_job_info: CompletedJobInfo | None = None
                for i, job in enumerate(self.jobs_being_safety_checked):
                    if job.job_info.id_ == message.job_id:
                        completed_job_info = self.jobs_being_safety_checked.pop(i)
                        break

                if completed_job_info is None or completed_job_info.job_result_images_base64 is None:
                    raise ValueError(
                        f"Expected to find a completed job with ID {message.job_id} but none was found",
                    )

                num_images_censored = 0
                num_images_csam = 0

                for i in range(len(completed_job_info.job_result_images_base64)):
                    replacement_image = message.safety_evaluations[i].replacement_image_base64
                    if replacement_image is not None:
                        completed_job_info.job_result_images_base64[i] = replacement_image
                        num_images_censored += 1
                        if message.safety_evaluations[i].is_csam:
                            num_images_csam += 1

                logger.info(f"Job {message.job_id} had {num_images_censored} images censored")

                if num_images_censored > 0:
                    completed_job_info.censored = True
                    if num_images_csam > 0:
                        completed_job_info.state = GENERATION_STATE.csam
                    else:
                        completed_job_info.state = GENERATION_STATE.censored
                else:
                    completed_job_info.censored = False

                self.completed_jobs.append(completed_job_info)

    def preload_models(self) -> None:
        """Preload models that are likely to be used soon."""

        # Starting from the left of the deque, preload models that are not yet loaded up to the
        # number of inference processes
        # that are available
        num_already_loaded_model = 0
        for job in self.job_deque:
            if job.model is None:
                raise ValueError(f"job.model is None ({job})")

            if self._horde_model_map.is_model_loaded(job.model) or self._horde_model_map.is_model_loading(job.model):
                num_already_loaded_model += 1
                continue

            if num_already_loaded_model >= self._process_map.num_inference_processes():
                break

            available_process = self._process_map.get_first_available_inference_process()

            if available_process is None:
                return

            logger.debug(f"Preloading model {job.model} on process {available_process.process_id}")
            logger.debug(f"Available inference processes: {self._process_map}")
            logger.debug(f"Horde model map: {self._horde_model_map}")

            will_load_loras = job.payload.loras is not None and len(job.payload.loras) > 0
            seamless_tiling_enabled = job.payload.tiling is not None and job.payload.tiling

            available_process.pipe_connection.send(
                HordePreloadInferenceModelMessage(
                    control_flag=HordeControlFlag.PRELOAD_MODEL,
                    horde_model_name=job.model,
                    will_load_loras=will_load_loras,
                    seamless_tiling_enabled=seamless_tiling_enabled,
                ),
            )

            self._horde_model_map.update_entry(
                horde_model_name=job.model,
                load_state=ModelLoadState.LOADING,
                process_id=available_process.process_id,
            )

            self._process_map.update_entry(
                process_id=available_process.process_id,
                loaded_horde_model_name=job.model,
            )

            break

    def start_inference(self) -> None:
        """Start inference for the next job in the deque, if possible."""

        if len(self.jobs_in_progress) >= self.max_concurrent_inference_processes:
            return

        # Get the first job in the deque that is not already in progress
        next_job: ImageGenerateJobPopResponse | None = None
        for job in self.job_deque:
            if job in self.jobs_in_progress:
                continue
            next_job = job
            break

        if next_job is None:
            return

        if next_job.model is None:
            raise ValueError(f"next_job.model is None ({next_job})")

        if self._horde_model_map.is_model_loaded(next_job.model):
            process_with_model = self._process_map.get_process_by_horde_model_name(next_job.model)

            if process_with_model is None:
                logger.error(
                    f"Expected to find a process with model {next_job.model} but none was found",
                )
                logger.debug(f"Horde model map: {self._horde_model_map}")
                logger.debug(f"Process map: {self._process_map}")
                return

            if not process_with_model.can_accept_job():
                return

            # Unload all models from vram from any other process that isn't running a job
            for process_info in self._process_map.values():
                if process_info.process_id == process_with_model.process_id:
                    continue

                if process_info.is_process_busy():
                    continue

                if process_info.loaded_horde_model_name is None:
                    continue

                process_info.pipe_connection.send(
                    HordeControlModelMessage(
                        control_flag=HordeControlFlag.UNLOAD_MODELS_FROM_VRAM,
                        horde_model_name=process_info.loaded_horde_model_name,
                    ),
                )
                time.sleep(0.1)

            self.jobs_in_progress.append(next_job)
            process_with_model.pipe_connection.send(
                HordeInferenceControlMessage(
                    control_flag=HordeControlFlag.START_INFERENCE,
                    horde_model_name=next_job.model,
                    job_info=next_job,
                ),
            )

    def unload_from_ram(self, process_id: int) -> None:
        """Unload models from a process, either from VRAM or both VRAM and system RAM."""

        if process_id not in self._process_map:
            raise ValueError(f"process_id {process_id} is not in the process map")

        process_info = self._process_map[process_id]

        if process_info.loaded_horde_model_name is None:
            raise ValueError(f"process_id {process_id} is not loaded with a model")

        if not self._horde_model_map.is_model_loaded(process_info.loaded_horde_model_name):
            raise ValueError(f"process_id {process_id} is loaded with a model that is not loaded")

        process_info.pipe_connection.send(
            HordeControlModelMessage(
                control_flag=HordeControlFlag.UNLOAD_MODELS_FROM_RAM,
                horde_model_name=process_info.loaded_horde_model_name,
            ),
        )

        self._horde_model_map.update_entry(
            horde_model_name=process_info.loaded_horde_model_name,
            load_state=ModelLoadState.ON_DISK,
            process_id=process_id,
        )

        self._process_map.update_entry(
            process_id=process_id,
            loaded_horde_model_name=None,
        )

    def unload_models(self) -> None:
        """Unload models that are no longer needed and would use above the limit specified."""

        for process_info in self._process_map.values():
            if process_info.is_process_busy():
                continue

            if process_info.loaded_horde_model_name is None:
                continue

            if self._horde_model_map.is_model_loading(process_info.loaded_horde_model_name):
                continue

            next_n_models: set[str] = set()
            jobs_traversed = 0
            while len(next_n_models) < self.max_concurrent_inference_processes:
                if jobs_traversed >= len(self.job_deque):
                    break

                model_name = self.job_deque[jobs_traversed].model

                if model_name is None:
                    raise ValueError(f"job_deque[{jobs_traversed}].model is None")

                if model_name not in next_n_models:
                    next_n_models.add(model_name)

                jobs_traversed += 1

            if process_info.loaded_horde_model_name in next_n_models:
                continue

            if self.get_process_total_ram_usage() > self.target_ram_bytes_used:
                self.unload_from_ram(process_info.process_id)

    def start_evaluate_safety(self) -> None:
        if len(self.jobs_pending_safety_check) == 0:
            return

        safety_process = self._process_map.get_first_available_safety_process()

        if safety_process is None:
            return

        completed_job_info = self.jobs_pending_safety_check[0]

        if completed_job_info.job_result_images_base64 is None:
            raise ValueError("completed_job_info.job_result_images_base64 is None")

        if len(completed_job_info.job_result_images_base64) > 1:
            raise NotImplementedError("Only single image jobs are supported right now")  # TODO

        if completed_job_info.job_info.id_ is None:
            raise ValueError("completed_job_info.job_info.id_ is None")

        if completed_job_info.job_info.model is None:
            raise ValueError("completed_job_info.job_info.model is None")

        if self.stable_diffusion_reference is None:
            raise ValueError("stable_diffusion_reference is None")

        if completed_job_info.job_info.payload.prompt is None:
            raise ValueError("completed_job_info.job_info.payload.prompt is None")

        self.jobs_pending_safety_check.remove(completed_job_info)
        self.jobs_being_safety_checked.append(completed_job_info)

        safety_process.pipe_connection.send(
            HordeSafetyControlMessage(
                control_flag=HordeControlFlag.EVALUATE_SAFETY,
                job_id=completed_job_info.job_info.id_,
                images_base64=completed_job_info.job_result_images_base64,
                prompt=completed_job_info.job_info.payload.prompt,
                censor_nsfw=completed_job_info.job_info.payload.use_nsfw_censor,
                sfw_worker=not self.bridge_data.nsfw,
                horde_model_info=self.stable_diffusion_reference.root[completed_job_info.job_info.model].model_dump(),
                # TODO: update this to use a class instead of a dict?
            ),
        )

    def base64_image_to_stream_buffer(self, image_base64: str) -> BytesIO:
        """Convert a base64 image to a BytesIO stream buffer."""
        image_as_pil = PIL.Image.open(BytesIO(base64.b64decode(image_base64)))
        image_buffer = BytesIO()
        image_as_pil.save(
            image_buffer,
            format="WebP",
            quality=95,  # FIXME # TODO
            method=6,
        )

        return image_buffer

    async def api_submit_job(
        self,
    ) -> None:
        if len(self.completed_jobs) == 0:
            return

        completed_job_info = self.completed_jobs[0]
        job_info = completed_job_info.job_info

        submit_job_request_type = job_info.get_follow_up_default_request_type()

        if completed_job_info.job_result_images_base64 is None:
            raise ValueError("completed_job_info.job_result_images_base64 is None")

        if len(completed_job_info.job_result_images_base64) > 1:
            raise NotImplementedError("Only single image jobs are supported right now")

        if job_info.id_ is None:
            raise ValueError("job_info.id_ is None")

        if job_info.payload.seed is None:
            raise ValueError("job_info.payload.seed is None")

        if job_info.r2_upload is None:
            raise ValueError("job_info.r2_upload is None")

        if completed_job_info.censored is None:
            raise ValueError("completed_job_info.censored is None")

        # TODO: n_iter support

        try:
            image_in_buffer = self.base64_image_to_stream_buffer(completed_job_info.job_result_images_base64[0])

            # TODO: This would be better (?) if we could use aiohttp instead of requests
            # except for the fact that it causes S3 to return a 403 Forbidden error

            # async with self._aiohttp_session.put(
            #     yarl.URL(job_info.r2_upload, encoded=True),
            #     data=image_in_buffer.getvalue(),
            # ) as response:
            #     if response.status != 200:
            #         logger.error(f"Failed to upload image to R2: {response}")
            #         return

            response = requests.put(job_info.r2_upload, data=image_in_buffer.getvalue())

            if response.status_code != 200:
                logger.error(f"Failed to upload image to R2: {response}")
                return

            submit_job_request = submit_job_request_type(
                apikey=self.bridge_data.api_key,
                id=job_info.id_,
                seed=int(job_info.payload.seed),
                generation="R2",  # TODO # FIXME
                state=completed_job_info.state,
                censored=completed_job_info.censored,
            )

            job_submit_response = await self.horde_client_session.submit_request(submit_job_request, JobSubmitResponse)

            if isinstance(job_submit_response, RequestErrorResponse):
                logger.error(f"Failed to submit job (API Error): {job_submit_response}")
                return

            logger.info(
                f"Submitted job {job_info.id_} (model: {job_info.model}) for {job_submit_response.reward} kudos.",
            )
            async with self._completed_jobs_lock:
                self.completed_jobs.remove(completed_job_info)
        except Exception as e:
            logger.error(f"Failed to submit job (Unexpected Error): {e}")
            return

    _testing_max_jobs = 10000
    _testing_jobs_added = 0
    _testing_job_queue_length = 1

    _default_job_pop_frequency = 1.0
    _error_job_pop_frequency = 5.0
    _job_pop_frequency = 1.0
    _last_job_pop_time = 0.0

    async def api_job_pop(self) -> None:
        """If the job deque is not full, add any jobs that are available to the job deque."""
        if len(self.job_deque) >= self.bridge_data.queue_size + 1:  # FIXME?
            return

        if self._testing_jobs_added >= self._testing_max_jobs:
            return

        if time.time() - self._last_job_pop_time < self._job_pop_frequency:
            return

        self._last_job_pop_time = time.time()

        # dummy_jobs = get_n_dummy_jobs(1)
        # async with self._job_deque_lock:
        #     self.job_deque.extend(dummy_jobs)
        # logger.debug(f"Added {len(dummy_jobs)} dummy jobs to the job deque")
        # # log a list of the current model names in the deque
        # logger.debug(f"Current models in job deque: {[job.model for job in self.job_deque]}")

        try:
            job_pop_request = ImageGenerateJobPopRequest(
                apikey=self.bridge_data.api_key,
                name=self.bridge_data.dreamer_worker_name,
                bridge_agent="AI Horde Worker:23:tazlin reGen testing",
                bridge_version=23,  # TODO TIs broken
                models=self.bridge_data.image_models_to_load,
                nsfw=self.bridge_data.nsfw,
                threads=self.max_concurrent_inference_processes,
                max_pixels=self.bridge_data.max_power * 8 * 64 * 64,
                require_upfront_kudos=self.bridge_data.require_upfront_kudos,
                allow_img2img=self.bridge_data.allow_img2img,
                allow_painting=self.bridge_data.allow_inpainting,
                allow_unsafe_ipaddr=self.bridge_data.allow_unsafe_ip,
                allow_post_processing=self.bridge_data.allow_post_processing,
                allow_controlnet=self.bridge_data.allow_controlnet,
                allow_lora=False,  # TODO loras broken
            )

            job_pop_response = await self.horde_client_session.submit_request(
                job_pop_request,
                ImageGenerateJobPopResponse,
            )

            if isinstance(job_pop_response, RequestErrorResponse):
                logger.error(f"Failed to pop job (API Error): {job_pop_response}")
                self._job_pop_frequency = self._error_job_pop_frequency
                return
        except Exception as e:
            logger.error(f"Failed to pop job (Unexpected Error): {e}")
            self._job_pop_frequency = self._error_job_pop_frequency
            return

        self._job_pop_frequency = self._default_job_pop_frequency

        if job_pop_response.id_ is None:
            logger.info(
                f"No job available. (Skipped reasons: {job_pop_response.skipped.model_dump(exclude_defaults=True)})",
            )
            return

        logger.info(f"Popped job {job_pop_response.id_} (model: {job_pop_response.model})")

        async with self._job_deque_lock:
            self.job_deque.append(job_pop_response)
            self._testing_jobs_added += 1

        with open(f"testing/{str(job_pop_response.id_)}", "w") as f:
            f.write(job_pop_response.model_dump_json(by_alias=True))

    _user_info_failed = False
    _user_info_failed_reason: str | None = None

    async def api_get_user_info(self) -> None:
        request = FindUserRequest(apikey=self.bridge_data.api_key)
        try:
            response = await self.horde_client_session.submit_request(request, FindUserResponse)
            if isinstance(response, RequestErrorResponse):
                logger.error(f"Failed to get user info (API Error): {response}")
                self._user_info_failed = True
                return
            if self.user_info is None:
                logger.info(f"Got user info: {response}")  # FIXME

            self.user_info = response
            self._user_info_failed = False
            self._user_info_failed_reason = None

            if self.user_info.kudos_details is not None:
                logger.debug(f"Kudos Accumulated: {self.user_info.kudos_details.accumulated }")

        except ClientError as e:
            self._user_info_failed = True
            self._user_info_failed_reason = f"HTTP error (({type(e).__name__}) {e})"

        except Exception as e:
            self._user_info_failed = True
            self._user_info_failed_reason = f"Unexpected error (({type(e).__name__}) {e})"

        finally:
            if self._user_info_failed:
                logger.debug(f"Failed to get user info: {self._user_info_failed_reason}")
            await logger.complete()

    async def _api_call_loop(self) -> None:
        """Main loop for the API calls."""
        logger.debug("In _api_call_loop")
        self._aiohttp_session = ClientSession(requote_redirect_url=True)
        async with self._aiohttp_session as aiohttp_session:
            self.horde_client_session = AIHordeAPIAsyncClientSession(aiohttp_session=aiohttp_session)
            self.horde_client = AIHordeAPIAsyncSimpleClient(
                aiohttp_session=None,
                horde_client_session=self.horde_client_session,
            )
            async with self.horde_client_session:
                while True:
                    with logger.catch():
                        if self._user_info_failed:
                            await asyncio.sleep(5)

                        tasks = [self.api_job_pop(), self.api_submit_job()]

                        if self._last_get_user_info_time + self._api_get_user_info_interval < time.time():
                            self._last_get_user_info_time = time.time()
                            # tasks.append(self.api_get_user_info())

                        if len(tasks) > 0:
                            await asyncio.gather(*tasks, return_exceptions=True)

                            for task in tasks:
                                if isinstance(task, Exception):
                                    logger.exception(f"Task failed: {task}")

                            if self._user_info_failed:
                                logger.error("The server failed to respond. Is the horde or your internet down?")

                    await asyncio.sleep(self._api_call_loop_interval)

    async def _process_control_loop(self) -> None:
        self.start_safety_processes()
        self.start_inference_processes()

        while True:
            if self.stable_diffusion_reference is None:
                return

            # We don't want to pop jobs from the deque while we are adding jobs to it
            # TODO: Is this necessary?
            async with self._job_deque_lock, self._jobs_safety_check_lock, self._completed_jobs_lock:
                self.receive_and_handle_process_messages()

            if len(self.jobs_pending_safety_check) > 0:
                async with self._jobs_safety_check_lock:
                    self.start_evaluate_safety()

            if self.is_time_for_shutdown():
                break

            if self.is_free_inference_process_available() and len(self.job_deque) > 0:
                self.preload_models()
                self.start_inference()
                self.unload_models()

            await asyncio.sleep(self._loop_interval)

        logger.info("Shutting down process manager")

    async def _main_loop(self) -> None:
        # Run both loops concurrently
        await asyncio.gather(
            asyncio.create_task(self._process_control_loop(), name="process_control_loop"),
            asyncio.create_task(self._api_call_loop(), name="api_call_loop"),
        )

    def start(self) -> None:
        """Start the process manager."""
        asyncio.run(self._main_loop())