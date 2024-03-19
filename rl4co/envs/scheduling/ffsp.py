import itertools

from math import factorial
from typing import Optional

import torch

from tensordict.tensordict import TensorDict
from torchrl.data import (
    BoundedTensorSpec,
    CompositeSpec,
    UnboundedContinuousTensorSpec,
    UnboundedDiscreteTensorSpec,
)

from rl4co.envs.common.base import RL4COEnvBase


class IndexTables:
    def __init__(self, num_stage, num_machine, flatten_stages, device):
        # Init stage and machine mapping table
        self.num_stage = num_stage
        self.num_machine = num_machine
        self.flatten_stages = flatten_stages
        self._reset(device)

    def _reset(self, device):
        self.stage_table = torch.arange(
            self.num_stage, dtype=torch.long, device=device
        ).repeat_interleave(self.num_machine)

        self.machine_table = torch.arange(
            self.num_machine * self.num_stage, dtype=torch.long, device=device
        )

        if self.flatten_stages:
            self.stage_machine_table = self.machine_table
        else:
            self.stage_machine_table = torch.arange(
                self.num_machine, dtype=torch.long, device=device
            ).repeat(self.num_stage)

        self.augmented = False

    def augment_machine_tables(self, td, num_starts):
        assert num_starts <= factorial(
            self.num_machine
        ), f"at most {factorial(self.num_machine)} starts possible"
        assert num_starts > 1, "augmentation makes only sense for multistart"
        bs = td.size(0)

        if self.augmented:
            # NOTE this should be failsafe through _reset() fn called in env._reset()
            assert bs == self.machine_table.size(
                0
            ), "data and machine table not compatible"
            return

        # determine the increment of machine ids between stages, i.e. [0,4,8]
        # for instances with 4 machines and three stages
        start_sub_ids = torch.tensor(
            list(range(0, self.num_machine * self.num_stage, self.num_machine)),
            dtype=torch.long,
            device=td.device,
        ).repeat_interleave(self.num_machine)
        # generate all possible permutations of the machine ids and add the stage increment to it
        # (num_permutations, total_machines)
        permutations = torch.tensor(
            list(itertools.permutations(list(range(self.num_machine)))),
            dtype=torch.long,
            device=td.device,
        ).repeat(1, self.num_stage)
        # (bs*POMO, total_machines)
        self.machine_table = (
            permutations[:num_starts].repeat_interleave(bs, dim=0) + start_sub_ids[None]
        )
        if self.flatten_stages:
            # when flatting stages, every machine in each stage is treated as a distinct entity (no shared embeddings)
            # Therefore, all machine need a unique index which is the same as the machine table
            self.stage_machine_table = self.machine_table
        else:
            # when we do not flatten the stages, machines of different stages with the same subtime index
            # share an embedding. In this case, they need the same index (i.e. leave out the stage increment)
            self.stage_machine_table = permutations[:num_starts].repeat_interleave(
                bs, dim=0
            )

        self.augmented = True

    def get_stage_index(self, sub_time_idx):
        return self.stage_table[sub_time_idx]

    def get_machine_index(self, sub_time_idx, idx=None):
        if self.augmented:
            assert idx is not None
            return self.machine_table[idx, sub_time_idx]
        else:
            return self.machine_table[sub_time_idx]

    def get_stage_machine_index(self, sub_time_idx, idx=None):
        if self.augmented:
            assert idx is not None
            return self.stage_machine_table[idx, sub_time_idx]
        else:
            return self.stage_machine_table[sub_time_idx]


class FFSPEnv(RL4COEnvBase):
    """Flexible Flow Shop Problem (FFSP) environment.
    The goal is to schedule a set of jobs on a set of machines such that the makespan is minimized.

    Args:
        num_stage: number of stages
        num_machine: number of machines in each stage
        num_job: number of jobs
        min_time: minimum processing time of a job
        max_time: maximum processing time of a job
        batch_size: batch size of the problem

    Note:
        - [IMPORTANT] This version of ffsp requires the number of machines in each stage to be the same
    """

    name = "ffsp"

    def __init__(
        self,
        num_stage: int,
        num_machine: int,
        num_job: int,
        min_time: int = 2,
        max_time: int = 10,
        flatten_stages: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.num_stage = num_stage
        self.num_machine = num_machine
        self.num_machine_total = num_stage * num_machine
        self.num_job = num_job
        self.min_time = min_time
        self.max_time = max_time
        self.flatten_stages = flatten_stages
        # self.tables = IndexTables(num_stage, num_machine, flatten_stages, self.device)#
        self.step_cnt = None

    # TODO make envs implement get_num_starts and select_start_nodes functions
    # def get_num_starts(self, td):
    #     return factorial(self.num_machine)

    # def select_start_nodes(self, td, num_starts):
    #     self.tables.augment_machine_tables(num_starts)
    #     selected = torch.full((num_starts * td.size(0),), self.num_job)
    #     return selected

    def _move_to_next_machine(self, td):
        batch_size = td.batch_size
        batch_idx = torch.arange(*batch_size, dtype=torch.long, device=td.device)

        time_idx = td["time_idx"]
        machine_idx = td["machine_idx"]
        sub_time_idx = td["sub_time_idx"]

        machine_wait_step = td["machine_wait_step"]
        job_wait_step = td["job_wait_step"]
        job_location = td["job_location"]

        ready = torch.flatten(td["done"])
        idx = torch.flatten(batch_idx)
        # select minibatch instances that need updates (are not done)
        idx = idx[~ready]

        while ~ready.all():
            # increment the stage-machine counter
            new_sub_time_idx = sub_time_idx[idx] + 1
            # increment time if all machines-stage combinations have been candidates
            step_time_required = new_sub_time_idx == self.num_machine_total
            time_idx[idx] += step_time_required.long()
            # in this case set the machine-stage counter to zero again
            new_sub_time_idx[step_time_required] = 0
            # update machine-stage counter
            sub_time_idx[idx] = new_sub_time_idx
            # determine current machine candidate
            new_machine_idx = self.tables.get_machine_index(idx, new_sub_time_idx)
            machine_idx[idx] = new_machine_idx

            # decrease machine wait time by 1 if instance transitioned to new time step
            machine_wait_steps = machine_wait_step[idx, :]
            machine_wait_steps[step_time_required, :] -= 1
            machine_wait_steps[machine_wait_steps < 0] = 0
            machine_wait_step[idx, :] = machine_wait_steps

            # decrease job wait time by 1 if instance transitioned to new time step
            job_wait_steps = job_wait_step[idx, :]
            job_wait_steps[step_time_required, :] -= 1
            job_wait_steps[job_wait_steps < 0] = 0
            job_wait_step[idx, :] = job_wait_steps
            # machine is ready if its wait time is zero
            machine_ready = machine_wait_step[idx, new_machine_idx] == 0
            # job is ready if the current stage matches the stage of the job and
            # its wait time is zero (no operation of previous stage is in process)
            new_stage_idx = self.tables.get_stage_index(new_sub_time_idx)
            job_ready_1 = job_location[idx, : self.num_job] == new_stage_idx[:, None]
            job_ready_2 = job_wait_step[idx, : self.num_job] == 0
            job_ready = (job_ready_1 & job_ready_2).any(dim=-1)
            # instance ready if at least one job and the current machine are ready
            ready = machine_ready & job_ready
            assert ready.shape == idx.shape
            idx = idx[~ready]

        return td.update(
            {
                "time_idx": time_idx,
                "sub_time_idx": sub_time_idx,
                "machine_idx": machine_idx,
                "machine_wait_step": machine_wait_step,
                "job_wait_step": job_wait_step,
            }
        )

    def pre_step(self, td: TensorDict) -> TensorDict:
        batch_size = td.batch_size
        batch_idx = torch.arange(*batch_size, dtype=torch.long, device=td.device)
        sub_time_idx = td["sub_time_idx"]
        # update machine index
        machine_idx = self.tables.get_machine_index(batch_idx, sub_time_idx)
        # update action mask and stage machine indx
        td = self._update_step_state(td)
        # perform some checks
        stage_machine_idx = td["stage_machine_idx"]
        stage_idx = td["stage_idx"]
        is_stage_one = stage_idx == 0
        assert torch.all(stage_machine_idx[is_stage_one] == machine_idx[is_stage_one])
        assert is_stage_one.all(), "call pre_step only at beginning of env"
        # return updated td
        return td

    def _update_step_state(self, td):
        batch_size = td.batch_size
        batch_idx = torch.arange(*batch_size, dtype=torch.long, device=td.device)

        sub_time_idx = td["sub_time_idx"]
        job_location = td["job_location"]
        job_wait_step = td["job_wait_step"]
        if len(td["done"].shape) == 2:
            done = td["done"].squeeze(1)
        else:
            done = td["done"]

        # update stage
        stage_idx = self.tables.get_stage_index(sub_time_idx)
        stage_machine_idx = self.tables.get_stage_machine_index(batch_idx, sub_time_idx)

        job_loc = job_location[:, : self.num_job]
        job_wait_time = job_wait_step[:, : self.num_job]
        # determine if job can be scheduled in current stage
        # (i.e. previous stages are completed)
        job_in_stage = job_loc == stage_idx[:, None]
        job_not_waiting = job_wait_time == 0
        # job can be scheduled if in current stage and not waiting
        job_available = job_in_stage & job_not_waiting
        # determine instance for which waiting is allowed. This is the case if either
        # 1.) any of its jobs need to be scheduled in a previous stage,
        # 2.) any of the jobs wait for an operation of the preceeding stage to finish
        # 3.) the instance is done.
        job_in_previous_stages = (job_loc < stage_idx[:, None]).any(dim=-1)
        job_waiting_in_stage = (job_in_stage & (job_wait_time > 0)).any(dim=-1)
        wait_allowed = job_in_previous_stages + job_waiting_in_stage + done

        job_enable = torch.cat((job_available, wait_allowed[:, None]), dim=-1)
        job_mask = torch.full_like(td["action_mask"], 0).masked_fill(job_enable, 1)
        assert torch.logical_or((job_mask[:, :-1].sum(1) > 0), done).all()
        return td.update(
            {
                "action_mask": job_mask,
                "stage_idx": stage_idx,
                "stage_machine_idx": stage_machine_idx,
            }
        )

    def _step(self, td: TensorDict) -> TensorDict:
        self.step_cnt += 1
        batch_size = td.batch_size
        batch_idx = torch.arange(*batch_size, dtype=torch.long, device=td.device)

        # job_idx is the action from the model
        job_idx = td["action"]
        time_idx = td["time_idx"]
        machine_idx = td["machine_idx"]

        # create new td to avoid incplace ops and gradient problems resulting from this
        # td = td.clone()

        # increment the operation counter of the selected job
        td["job_location"][batch_idx, job_idx] += 1
        # td["job_location"][:, :-1].clip_(0, self.num_stage)
        # assert (td["job_location"][:, : self.num_job] <= self.num_stage).all()
        # insert start time of the selected job in the schedule
        td["schedule"][batch_idx, machine_idx, job_idx] = time_idx
        # get the duration of the selected job
        job_length = td["job_duration"][batch_idx, job_idx, machine_idx]
        # TODO remove
        # test_job_length = torch.cat((td["cost_matrix"], torch.zeros_like(td["cost_matrix"][:, :1, :, :])), dim=1)[batch_idx, job_idx, stage_machine_idx, stage_idx]
        # assert torch.all(test_job_length == job_length)
        # set the number of time steps until the selected machine is available again
        td["machine_wait_step"][batch_idx, machine_idx] = job_length

        # set the number of time steps until the next operation of the job can be started
        td["job_wait_step"][batch_idx, job_idx] = job_length
        # determine whether all jobs are scheduled
        td["done"] = (td["job_location"][:, : self.num_job] == self.num_stage).all(dim=-1)

        if td["done"].all():
            pass
        else:
            td = self._move_to_next_machine(td)
            td = self._update_step_state(td)

        if td["done"].all():
            # determine end times of ops by adding the durations to their start times
            end_schedule = td["schedule"] + td["job_duration"].permute(0, 2, 1)
            # exclude dummy job and determine the makespan per job
            end_time_max, _ = end_schedule[:, :, : self.num_job].max(dim=-1)
            # determine the max makespan of all jobs
            end_time_max, _ = end_time_max.max(dim=-1)
            reward = -end_time_max.to(torch.float32)
            td.set("reward", reward)

        return td

    def _reset(
        self, td: Optional[TensorDict] = None, batch_size: Optional[list] = None
    ) -> TensorDict:
        """
        Args:

        Returns:
            - stage_table [batch_size, num_stage * num_machine]
            - machine_table [batch_size, num_machine * num_stage]
            - stage_machine_idx [batch_size, num_stage * num_machine]
            - time_idx [batch_size]
            - sub_time_idx [batch_size]
            - batch_idx [batch_size]
            - machine_idx [batch_size]
            - schedule [batch_size, num_machine_total, num_job+1]
            - machine_wait_step [batch_size, num_machine_total]
            - job_location [batch_size, num_job+1]
            - job_wait_step [batch_size, num_job+1]
            - job_duration [batch_size, num_job+1, num_machine * num_stage]
        """
        if batch_size is None:
            batch_size = self.batch_size if td is None else td.batch_size

        if td is None or td.is_empty():
            td = self.generate_data(batch_size=batch_size)

        self.step_cnt = 0
        self.to(td.device)
        self.tables = _Stage_N_Machine_Index_Converter(td.device)
        # reset tables to undo the augmentation
        # self.tables._reset(device=self.device)
        self.tables.set_bs(batch_size[0])

        # Init index record tensor
        time_idx = torch.zeros(size=(*batch_size,), dtype=torch.long, device=self.device)
        sub_time_idx = torch.zeros(
            size=(*batch_size,), dtype=torch.long, device=self.device
        )

        # Scheduling status information
        schedule = torch.full(
            size=(*batch_size, self.num_machine_total, self.num_job + 1),
            dtype=torch.long,
            device=self.device,
            fill_value=-999999,
        )
        machine_wait_step = torch.zeros(
            size=(*batch_size, self.num_machine_total),
            dtype=torch.long,
            device=self.device,
        )
        job_location = torch.zeros(
            size=(*batch_size, self.num_job + 1),
            dtype=torch.long,
            device=self.device,
        )
        job_wait_step = torch.zeros(
            size=(*batch_size, self.num_job + 1),
            dtype=torch.long,
            device=self.device,
        )
        job_duration = torch.empty(
            size=(*batch_size, self.num_job + 1, self.num_machine * self.num_stage),
            dtype=torch.long,
            device=self.device,
        )
        if self.flatten_stages:
            assert (
                len(td["cost_matrix"].shape) == 3
            ), "cost matrix has shape other than (bs, jobs, ma_total)"
            job_duration[..., : self.num_job, :] = td["cost_matrix"]
        else:
            assert (
                len(td["cost_matrix"].shape) == 4
            ), "cost matrix has shape other than (bs, jobs, ma, stages)"
            job_duration[..., : self.num_job, :] = (
                td["cost_matrix"]
                .transpose(-2, -1)
                .contiguous()
                .view(*batch_size, self.num_job, self.num_machine_total)
            )
        job_duration[..., self.num_job, :] = 0

        # Finish status information
        reward = torch.full(
            size=(*batch_size,),
            dtype=torch.float32,
            device=self.device,
            fill_value=float("-inf"),
        )
        done = torch.full(
            size=(*batch_size,),
            dtype=torch.bool,
            device=self.device,
            fill_value=False,
        )

        action_mask = torch.ones(
            size=(*batch_size, self.num_job + 1), dtype=bool, device=self.device
        )
        action_mask[..., -1] = 0

        batch_idx = torch.arange(*batch_size, dtype=torch.long, device=td.device)
        stage_idx = self.tables.get_stage_index(sub_time_idx)
        machine_idx = self.tables.get_machine_index(batch_idx, sub_time_idx)
        stage_machine_idx = self.tables.get_stage_machine_index(batch_idx, sub_time_idx)

        return TensorDict(
            {
                # Index information
                "stage_idx": stage_idx,
                "time_idx": time_idx,
                "sub_time_idx": sub_time_idx,
                "machine_idx": machine_idx,
                "stage_machine_idx": stage_machine_idx,
                # Scheduling status information
                "schedule": schedule,
                "machine_wait_step": machine_wait_step,
                "job_location": job_location,
                "job_wait_step": job_wait_step,
                "job_duration": job_duration,
                # Finish status information
                "reward": reward,
                "done": done,
                "action_mask": action_mask,
            },
            batch_size=batch_size,
        )

    def _make_spec(self, td_params: TensorDict):
        self.observation_spec = CompositeSpec(
            time_idx=UnboundedDiscreteTensorSpec(
                shape=(1,),
                dtype=torch.int64,
            ),
            sub_time_idx=UnboundedDiscreteTensorSpec(
                shape=(1,),
                dtype=torch.int64,
            ),
            batch_idx=UnboundedDiscreteTensorSpec(
                shape=(1,),
                dtype=torch.int64,
            ),
            machine_idx=UnboundedDiscreteTensorSpec(
                shape=(1,),
                dtype=torch.int64,
            ),
            schedule=UnboundedDiscreteTensorSpec(
                shape=(self.num_machine_total, self.num_job + 1),
                dtype=torch.int64,
            ),
            machine_wait_step=UnboundedDiscreteTensorSpec(
                shape=(self.num_machine_total),
                dtype=torch.int64,
            ),
            job_location=UnboundedDiscreteTensorSpec(
                shape=(self.num_job + 1),
                dtype=torch.int64,
            ),
            job_wait_step=UnboundedDiscreteTensorSpec(
                shape=(self.num_job + 1),
                dtype=torch.int64,
            ),
            job_duration=UnboundedDiscreteTensorSpec(
                shape=(self.num_job + 1, self.num_machine * self.num_stage),
                dtype=torch.int64,
            ),
            shape=(),
        )
        self.action_spec = BoundedTensorSpec(
            shape=(1,),
            dtype=torch.int64,
            low=0,
            high=self.num_loc,
        )
        self.reward_spec = UnboundedContinuousTensorSpec(shape=(1,))
        self.done_spec = UnboundedDiscreteTensorSpec(shape=(1,), dtype=torch.bool)

    def get_reward(self, td, actions) -> TensorDict:
        return td["reward"]

    def generate_data(self, batch_size) -> TensorDict:
        # Batch size input check
        batch_size = [batch_size] if isinstance(batch_size, int) else batch_size

        # Init observation: running time of each job on each machine
        run_time = torch.randint(
            low=self.min_time,
            high=self.max_time,
            size=(*batch_size, self.num_job, self.num_machine, self.num_stage),
        ).to(self.device)

        if self.flatten_stages:
            run_time = (
                run_time.transpose(-2, -1)
                .contiguous()
                .view(*batch_size, self.num_job, self.num_machine_total)
            )

        return TensorDict(
            {
                "cost_matrix": run_time,
            },
            batch_size=batch_size,
        )

    def render(self, td: TensorDict, idx: int):
        import matplotlib.patches as patches
        import matplotlib.pyplot as plt

        job_durations = td["job_duration"][idx, :, :]
        # shape: (job, machine)
        schedule = td["schedule"][idx, :, :]
        # shape: (machine, job)

        total_machine_cnt = self.num_machine_total
        makespan = -td["reward"][idx].item()

        # Create figure and axes
        fig, ax = plt.subplots(figsize=(makespan / 3, 5))
        cmap = self._get_cmap(self.num_job)

        plt.xlim(0, makespan)
        plt.ylim(0, total_machine_cnt)
        ax.invert_yaxis()

        plt.plot([0, makespan], [4, 4], "black")
        plt.plot([0, makespan], [8, 8], "black")

        for machine_idx in range(total_machine_cnt):
            duration = job_durations[:, machine_idx]
            # shape: (job)
            machine_schedule = schedule[machine_idx, :]
            # shape: (job)

            for job_idx in range(self.num_job):
                job_length = duration[job_idx].item()
                job_start_time = machine_schedule[job_idx].item()
                if job_start_time >= 0:
                    # Create a Rectangle patch
                    rect = patches.Rectangle(
                        (job_start_time, machine_idx),
                        job_length,
                        1,
                        facecolor=cmap(job_idx),
                    )
                    ax.add_patch(rect)

        ax.grid()
        ax.set_axisbelow(True)
        plt.show()

    def _get_cmap(self, color_cnt):
        from matplotlib.colors import ListedColormap

        colors_list = [
            "red",
            "orange",
            "yellow",
            "green",
            "blue",
            "purple",
            "aqua",
            "aquamarine",
            "black",
            "blueviolet",
            "brown",
            "burlywood",
            "cadetblue",
            "chocolate",
            "coral",
            "cornflowerblue",
            "darkblue",
            "darkgoldenrod",
            "darkgreen",
            "darkgrey",
            "darkkhaki",
            "darkmagenta",
            "darkolivegreen",
            "darkorange",
            "darkorchid",
            "darkred",
            "darkslateblue",
            "darkslategrey",
            "darkturquoise",
            "darkviolet",
            "deeppink",
            "deepskyblue",
            "dimgrey",
            "dodgerblue",
            "forestgreen",
            "gold",
            "goldenrod",
            "gray",
            "greenyellow",
            "hotpink",
            "indianred",
            "khaki",
            "lawngreen",
            "magenta",
            "maroon",
            "mediumaquamarine",
            "mediumblue",
            "mediumorchid",
            "mediumpurple",
            "mediumspringgreen",
            "mediumturquoise",
            "mediumvioletred",
            "midnightblue",
            "navy",
            "olive",
            "olivedrab",
            "orangered",
            "orchid",
            "palegreen",
            "paleturquoise",
            "palevioletred",
            "pink",
            "plum",
            "powderblue",
            "rebeccapurple",
            "rosybrown",
            "royalblue",
            "saddlebrown",
            "sandybrown",
            "sienna",
            "silver",
            "skyblue",
            "slateblue",
            "springgreen",
            "steelblue",
            "tan",
            "teal",
            "thistle",
            "tomato",
            "turquoise",
            "violet",
            "yellowgreen",
        ]

        cmap = ListedColormap(colors_list, N=color_cnt)

        return cmap


class _Stage_N_Machine_Index_Converter:
    def __init__(self, device, flatten=False):
        machine_SUBindex_0 = torch.tensor(
            list(itertools.permutations([0, 1, 2, 3])), device=device
        )
        machine_SUBindex_1 = torch.tensor(
            list(itertools.permutations([0, 1, 2, 3])), device=device
        )
        machine_SUBindex_2 = torch.tensor(
            list(itertools.permutations([0, 1, 2, 3])), device=device
        )

        starting_SUBindex = [0, 4, 8]
        machine_order_0 = machine_SUBindex_0 + starting_SUBindex[0]
        machine_order_1 = machine_SUBindex_1 + starting_SUBindex[1]
        machine_order_2 = machine_SUBindex_2 + starting_SUBindex[2]
        self.machine_table = torch.cat(
            (machine_order_0, machine_order_1, machine_order_2), dim=1
        )
        # machine_table.shape: (pomo, total_machine)
        if not flatten:
            self.machine_SUBindex_table = torch.cat(
                (machine_SUBindex_0, machine_SUBindex_1, machine_SUBindex_2), dim=1
            )
        else:
            self.machine_SUBindex_table = self.machine_table.clone()
        # assert env.pomo_size == 1
        # self.machine_SUBindex_table = torch.tensor([[0,1,2,3,0,1,2,3,0,1,2,3]])
        # self.machine_table = torch.tensor([[0,1,2,3,4,5,6,7,8,9,10,11]])

        self.stage_table = torch.tensor(
            [0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2], dtype=torch.long, device=device
        )

    def set_bs(self, bs):
        self.bs = bs

    def get_stage_index(self, sub_time_idx):
        return self.stage_table[sub_time_idx]

    def get_machine_index(self, idx, sub_time_idx):
        pomo_idx = idx // self.bs

        return self.machine_table[pomo_idx, sub_time_idx]

    def get_stage_machine_index(self, idx, sub_time_idx):
        pomo_idx = idx // self.bs
        return self.machine_SUBindex_table[pomo_idx, sub_time_idx]
