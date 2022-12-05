#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import math
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

from fbpcp.service.storage import StorageService

from fbpcp.util.typing import checked_cast
from fbpcs.common.entity.pcs_mpc_instance import PCSMPCInstance
from fbpcs.infra.certificate.certificate_provider import CertificateProvider
from fbpcs.onedocker_binary_config import OneDockerBinaryConfig
from fbpcs.private_computation.entity.pcs_feature import PCSFeature
from fbpcs.private_computation.entity.private_computation_instance import (
    PrivateComputationInstance,
    PrivateComputationInstanceStatus,
    PrivateComputationRole,
)
from fbpcs.private_computation.service.argument_helper import get_tls_arguments
from fbpcs.private_computation.service.constants import DEFAULT_LOG_COST_TO_S3

from fbpcs.private_computation.service.mpc.mpc import (
    create_and_start_mpc_instance,
    get_updated_pc_status_mpc_game,
    map_private_computation_role_to_mpc_party,
    MPCService,
)

from fbpcs.private_computation.service.pid_utils import (
    get_metrics_filepath,
    get_pid_metrics,
    get_sharded_filepath,
)
from fbpcs.private_computation.service.private_computation_service_data import (
    PrivateComputationServiceData,
)
from fbpcs.private_computation.service.private_computation_stage_service import (
    PrivateComputationStageService,
)

# This constant array are calcualted by SAFETY_FACTOR and K_ANON
INTERSECTION_THRESHOLD = [
    270,
    407,
    547,
    688,
    832,
    970,
    1109,
    1256,
    1395,
    1546,
    1687,
    1827,
    1968,
    2123,
    2264,
    2406,
    2547,
    2689,
    2851,
    2993,
    3136,
]
# When SAFETY_FACTOR or K_ANON changes, INTERSECTION_THRESHOLD should be recalculated using the notebook in summay of this diff
SAFETY_FACTOR = 0.692
K_ANON = 100
TARGET_ROWS_UDP_THREAD = 250000
TARGET_ROWS_LIFT_THREAD = 100000


class SecureRandomShardStageService(PrivateComputationStageService):
    """Handles business logic for the SECURE_RANDOM_SHARDER stage

    Private attributes:
        _onedocker_binary_config_map: Stores a mapping from mpc game to OneDockerBinaryConfig (binary version and tmp directory)
        _mpc_svc: creates and runs MPC instances
        _log_cost_to_s3: if money cost of the computation will be logged to S3
        _container_timeout: optional duration in seconds before cloud containers timeout
    """

    def __init__(
        self,
        storage_svc: StorageService,
        onedocker_binary_config_map: DefaultDict[str, OneDockerBinaryConfig],
        mpc_service: MPCService,
        log_cost_to_s3: bool = DEFAULT_LOG_COST_TO_S3,
        container_timeout: Optional[int] = None,
    ) -> None:
        self._storage_svc = storage_svc
        self._onedocker_binary_config_map = onedocker_binary_config_map
        self._mpc_service = mpc_service
        self._log_cost_to_s3 = log_cost_to_s3
        self._container_timeout = container_timeout

    async def run_async(
        self,
        pc_instance: PrivateComputationInstance,
        server_certificate_provider: CertificateProvider,
        ca_certificate_provider: CertificateProvider,
        server_certificate_path: str,
        ca_certificate_path: str,
        server_ips: Optional[List[str]] = None,
    ) -> PrivateComputationInstance:
        """Runs the secure random shard stage service
        Args:
            pc_instance: the private computation instance to run secure random sharding with
            server_ips: only used by the partner role. These are the ip addresses of the publisher's containers.

        Returns:
            An updated version of pc_instance that stores an MPCInstance
        """
        logging.info(f"[{self}] Starting Secure Random Sharding.")
        game_args = await (
            self._get_secure_random_sharder_args(
                pc_instance,
                server_certificate_path,
                ca_certificate_path,
            )
        )

        if server_ips and len(server_ips) != len(game_args):
            raise ValueError(
                f"Unable to rerun secure random sharding compute because there is a mismatch between the number of server ips given ({len(server_ips)}) and the number of containers ({len(game_args)}) to be spawned."
            )

        logging.info(f"[{self}] Starting Secure Random Sharding.")

        stage_data = PrivateComputationServiceData.SECURE_RANDOM_SHARDER_DATA
        binary_name = stage_data.binary_name
        game_name = checked_cast(str, stage_data.game_name)

        binary_config = self._onedocker_binary_config_map[binary_name]
        should_wait_spin_up: bool = (
            pc_instance.infra_config.role is PrivateComputationRole.PARTNER
        )

        mpc_instance = await create_and_start_mpc_instance(
            mpc_svc=self._mpc_service,
            instance_id=pc_instance.infra_config.instance_id + "_secure_random_sharder",
            game_name=game_name,
            mpc_party=map_private_computation_role_to_mpc_party(
                pc_instance.infra_config.role
            ),
            num_containers=pc_instance.infra_config.num_pid_containers,
            binary_version=binary_config.binary_version,
            server_certificate_provider=server_certificate_provider,
            ca_certificate_provider=ca_certificate_provider,
            server_certificate_path=server_certificate_path,
            ca_certificate_path=ca_certificate_path,
            server_ips=server_ips,
            game_args=game_args,
            container_timeout=self._container_timeout,
            repository_path=binary_config.repository_path,
            wait_for_containers_to_start_up=should_wait_spin_up,
        )
        pc_instance.infra_config.instances.append(
            PCSMPCInstance.from_mpc_instance(mpc_instance)
        )
        return pc_instance

    def get_status(
        self,
        pc_instance: PrivateComputationInstance,
    ) -> PrivateComputationInstanceStatus:
        """Gets the latest PrivateComputationInstance status.

        Arguments:
            pc_instance: The private computation instance that is being updated

        Returns:
            The latest status for private computation instance
        """
        return get_updated_pc_status_mpc_game(pc_instance, self._mpc_service)

    async def _get_secure_random_sharder_args(
        self,
        pc_instance: PrivateComputationInstance,
        server_certificate_path: str,
        ca_certificate_path: str,
    ) -> List[Dict[str, Any]]:
        """Gets the game args passed to game binaries by onedocker

        When onedocker spins up containers to run games, it unpacks a dictionary containing the
        arguments required by the game binary being ran. This function prepares that dictionary.

        Args:
            pc_instance: the private computation instance to generate game args for

        Returns:
            MPC game args to be used by onedocker
        """

        id_combiner_output_path = pc_instance.data_processing_output_path + "_combine"
        num_secure_random_sharder_containers = (
            pc_instance.infra_config.num_pid_containers
        )

        output_shards_base_path = pc_instance.secure_random_sharder_output_base_path

        tls_args = get_tls_arguments(
            pc_instance.has_feature(PCSFeature.PCF_TLS),
            server_certificate_path,
            ca_certificate_path,
        )

        union_sizes, intersection_sizes = await self.get_union_stats(pc_instance)

        shards_per_file = self.get_dynamic_shards_num(union_sizes, intersection_sizes)
        self.setup_udp_lift_stages(
            pc_instance, union_sizes, intersection_sizes, shards_per_file
        )
        for i in range(num_secure_random_sharder_containers):
            logging.info(
                f"[{self}] {i}-th ID spine stats: union_size is {union_sizes[i]}, intersection_size is {intersection_sizes[i]}, shards_per_file is {shards_per_file[i]}"
            )

        cmd_args_list = []
        for shard_index in range(num_secure_random_sharder_containers):
            path_to_input_shard = get_sharded_filepath(
                id_combiner_output_path, shard_index
            )
            args_per_shard: Dict[str, Any] = {
                "input_filename": path_to_input_shard,
                "output_base_path": output_shards_base_path,
                "file_start_index": sum(shards_per_file[0:shard_index]),
                "num_output_files": shards_per_file[shard_index],
                # TODO T133330151 Add run_id support to PL UDP binary
                # "run_id": private_computation_instance.infra_config.run_id,
                **tls_args,
            }
            cmd_args_list.append(args_per_shard)
        return cmd_args_list

    async def get_union_stats(
        self,
        pc_instance: PrivateComputationInstance,
    ) -> Tuple[List[int], List[int]]:
        """
        Return union size and the intersection size in each shard from the PID metric logging.
        """
        spine_path = pc_instance.pid_stage_output_spine_path
        num_pid_containers = pc_instance.infra_config.num_pid_containers

        union_sizes = []
        intersection_sizes = []

        for shard in range(num_pid_containers):
            pid_match_metric_dict = await get_pid_metrics(
                self._storage_svc, spine_path, shard
            )
            pid_match_metric_path = get_metrics_filepath(spine_path, shard)
            if "union_file_size" not in pid_match_metric_dict:
                raise ValueError(
                    f"PID metrics file doesn't have union_file_size in {pid_match_metric_path}"
                )
            if "partner_input_size" not in pid_match_metric_dict:
                raise ValueError(
                    f"PID metrics file doesn't have partner_input_size in {pid_match_metric_path}"
                )
            if "publisher_input_size" not in pid_match_metric_dict:
                raise ValueError(
                    f"PID metrics file doesn't have publisher_input_size in {pid_match_metric_path}"
                )
            union_sizes.append(pid_match_metric_dict["union_file_size"])
            intersection_sizes.append(
                pid_match_metric_dict["partner_input_size"]
                + pid_match_metric_dict["publisher_input_size"]
                - pid_match_metric_dict["union_file_size"]
            )
        return union_sizes, intersection_sizes

    def setup_udp_lift_stages(
        self,
        pc_instance: PrivateComputationInstance,
        union_sizes: List[int],
        intersection_sizes: List[int],
        num_shards_per_file: List[int],
    ) -> None:
        total_num_of_shards = sum(num_shards_per_file)
        total_rows_of_intersection = sum(intersection_sizes)
        if total_rows_of_intersection == 0:
            logging.warning(f"[{self}] total intersection size is 0!")
            pc_instance.infra_config.num_udp_containers = math.ceil(
                total_num_of_shards / pc_instance.infra_config.mpc_compute_concurrency
            )
            pc_instance.infra_config.num_lift_containers = 1
            return
        pc_instance.infra_config.num_secure_random_shards = total_num_of_shards
        pc_instance.infra_config.num_udp_containers = math.ceil(
            total_num_of_shards / pc_instance.infra_config.mpc_compute_concurrency
        )
        rows_per_file = math.floor(total_rows_of_intersection / total_num_of_shards)
        files_per_lift_thread = math.ceil(TARGET_ROWS_LIFT_THREAD / rows_per_file)
        pc_instance.infra_config.num_lift_containers = math.ceil(
            total_num_of_shards
            / (pc_instance.infra_config.mpc_compute_concurrency * files_per_lift_thread)
        )

    # The number of shares per file is determined by the minimun of the following two parameters:
    # 1) INTERSECTION_THRESHOLD, the upper bound for shards per files calculated from k_anon requirements
    # 2) target number of shards per files, calculated by union_size / target_rows_per_thread
    # A note here: The first one is chosen only when the intersection rate is extremely low (< 0.1%)
    def get_dynamic_shards_num(
        self, union_sizes: List[int], intersection_sizes: List[int]
    ) -> List[int]:
        shards_by_union_sizes = []
        for union_size in union_sizes:
            shards_by_union_sizes.append(math.ceil(union_size / TARGET_ROWS_UDP_THREAD))
        shards_by_intersection_sizes = []
        for intersection_size in intersection_sizes:
            # Check if K-anon violation occurs
            if intersection_size < K_ANON:
                logging.warning(
                    f"[{self}] intersection size {intersection_size} in file is smaller than K_ANON threshold {K_ANON}"
                )
            # Check if the intersection_size is in range of precalculated INTERSECTION_THRESHOLD
            # If so, use the corresponding number of shards
            # Otherwise, calculate the max number of shards using safty factor and k_anon
            if intersection_size <= INTERSECTION_THRESHOLD[-1]:
                num_shard = 1
                for threshold in INTERSECTION_THRESHOLD:
                    if intersection_size >= threshold:
                        num_shard += 1
                    else:
                        break
            else:
                num_shard = intersection_size // (K_ANON * SAFETY_FACTOR)
            shards_by_intersection_sizes.append(num_shard)
        return [
            min(i, j)
            for (i, j) in zip(shards_by_union_sizes, shards_by_intersection_sizes)
        ]
