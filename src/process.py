import sys
from mpi4py import MPI
import numpy as np
from .game_state import GameState
from .job import Job
from .utils import *
if sys.version_info[0] >= 3:
    from functools import reduce
    from queue import PriorityQueue
else:
    from Queue import PriorityQueue
import logging

class Process:
    """
    Class that defines the behavior what each process should do
    """
    IS_FINISHED = False

    def dispatch(self, job):
        """
        Given a particular kind of job, decide what to do with
        it, this can range from lookup, to distributing, to
        checking for recieving.
        """
        _dispatch_table = (
            self.finished,
            self.lookup,
            self.resolve,
            self.send_back,
            self.distribute,
            self.stats,
            self.check_for_updates
        )
        return _dispatch_table[job.job_type](job)

    def _queue_to_str(self, q):
        """
        For debugging purposes.
        Prints the job type for each job in the job queue.
        """
        return ' '
        # return ', '.join([JOB_TYPE_MAP[j.job_type] + " " + str(j.game_state.pos) for j in q.queue])

    def _log_work(self, work):
        check_for_updates = 'check_for_updates, check_for_updates'
        logging.info("Machine " + str(self.rank) + " has " + self._queue_to_str(self.work) + " lined up to work on")

    def run(self):
        """
        Main loop for each process
        """
        while not Process.IS_FINISHED:
            self._log_work(self.work)
            if self.rank == self.root and self.initial_pos.index in self.resolved:
                Process.IS_FINISHED = True
                logging.info('Finished')
                print(STATE_MAP[self.resolved[self.initial_pos.index]] + " in " + str(self.remote[self.initial_pos.index]) + " moves")
                statistics = []         # We store stats_dict from each node in the statistics list.
                for rank in range(self.world_size):
                    if rank == self.rank:
                        stat = self.stats_dict
                    else:
                        send_req = self.send(Job(Job.STATS), dest = rank)
                        stat = self.recv(source = rank)
                    statistics.append( (rank, stat) )
                with open('data/statistics.txt', 'w+') as f:
                    f.write(str(statistics))
                self.comm.Abort()
            if self.work.empty():
                self.add_job(Job(Job.CHECK_FOR_UPDATES))
            job = self.work.get()
            result = self.dispatch(job)
            if result is None: # Check for updates returns nothing.
                continue
            self.add_job(result)

    def __init__(self, rank, world_size, comm, NP=False):
        self.rank = rank
        self.world_size = world_size
        self.comm = comm

        if NP:
            self.send = self.comm.Send # send and recv redeclarations for brevity.
            self.recv = self.comm.Recv
            self.pos_size = GameState.INITIAL_POS.size
        else:
            self.send = self.comm.send
            self.recv = self.comm.recv
        self.initial_pos = GameState(GameState.INITIAL_POS)
        self.root = self.initial_pos.get_hash(self.world_size)

        self.work = PriorityQueue()
        self.resolved = {}
        self.remote = {}
        # As for recieving, should test them when appropriate
        # in the run loop.
        self.received = []
        # Keep a dictionary of "distributed tasks"
        # Should contain an id associated with the length of task.
        # For example, you distributed rank 0 has 4, you wish to
        # distribute 3, 2. Give it an id, like 1 and associate it
        # with length 2. Then once all the results have been received
        # you can compare the length, and then reduce the results.
        # solving this particular distributed task.
        self._id = 0              # Job id tracker.
        self._counter = {}        # A job_id -> Number of results
                                  # remaining.
        self._pending = {}        # job_id -> [ Job, GameStates, ... ]
                                  # Resolved.
        self.stats_dict = {}            # Dictionary continue statistics for process.
        self.stats_dict["num_lookups"] = 0

    def add_job(self, job):
        """
        Adds a job to the priority queue so it may be worked on at an
        appropriate time
        """
        self.work.put(job)

    def finished(self, job):
        """
        Occurs when the root node has detected that the game has been solved
        """
        IS_FINISHED = True

    def lookup(self, job):
        """
        Takes a GameState object and determines if it is in the
        resolved list. Returns the result if this is the case, None
        otherwise.
        """
        logging.info("Machine " + str(self.rank) + " looking up " + str(job.game_state.pos))
        self.stats_dict["num_lookups"] += 1
        try:
            res = self.resolved[job.game_state.index]
            rem = self.remote[job.game_state.index]
            logging.info("Position " + str(job.game_state.pos) + " has been resolved")
            job.game_state.state = res
            job.game_state.remoteness = rem
            return Job(Job.SEND_BACK, job.game_state, job.parent, job.job_id)
        except KeyError: # Not in dictionary.
            # Try to see if it is_primitive:
            if job.game_state.is_primitive():
                logging.info("Position " + str(job.game_state.pos) + " is primitive")
                self.remote[job.game_state.index] = PRIMITIVE_REMOTENESS
                job.game_state.remoteness = PRIMITIVE_REMOTENESS
                self.resolved[job.game_state.index] = job.game_state.primitive
                return Job(Job.SEND_BACK, job.game_state, job.parent, job.job_id)
            return Job(Job.DISTRIBUTE, job.game_state, job.parent, job.job_id)

    def _add_pending_state(self, job, children):
        # Refer to lines 179-187 for an explanation of why this
        # is done.
        self._pending[self._id] = [job]
        self._counter[self._id] = len(list(children))

    def _update_id(self):
        """
        Changes the id so there is no collision.
        """
        self._id += 1

    def distribute(self, job):
        """
        Given a gamestate distributes the results to the appropriate
        children.
        """
        children = list(job.game_state.expand())
        # Add new pending state information.
        logging.info('Found children ' + ', '.join([str(c.pos) for c in children]))
        self._add_pending_state(job, children)
        logging.info('Found children ' + ', '.join([str(c.pos) for c in children]))
        # Keep a list of the requests made by isend. Something may
        # fail, so we will need to worry about error checking at
        # some point.
        for child in children:
            new_job = Job(Job.LOOK_UP, child, self.rank, self._id)
            logging.info("Machine " + str(self.rank)
                       + " found child " + str(new_job.game_state.pos)
                       + ", sending to " + str(child.get_hash(self.world_size)))
            if NP:
                new_job_to_send = new_job.construct_numpy_representation() # Package job with NumPy for sending
                logging.debug("Preparing to send " + str(new_job_to_send) + " in distribute")
                self.send(new_job_to_send, dest = child.get_hash(self.world_size))
                logging.debug("Send successful")

            else:
                self.send(new_job, dest = child.get_hash(self.world_size))
        self._update_id()

    def stats(self, job):
        """
        Returns stats for this Process to the root Process.
        """
        self.send(self.stats_dict, dest = self.root)

    def check_for_updates(self, job):
        """
        Checks if there is new data from other Processes that needs to
        be received and prepares to recieve it if there is any new data.
        Returns True if there is new data to be recieved.
        Returns None if there is nothing to be recieved.
        """
        # Probe for any sources
        if self.comm.iprobe(source=MPI.ANY_SOURCE):
            # If there are sources recieve them.
            if NP:
                new_job_data = np.arange(POS_START_INDEX + self.pos_size)
                logging.debug("Preparing to receive")
                self.recv([new_job_data, game_module.board_state_element_type], source=MPI.ANY_SOURCE)
                self.received.append(Job.construct_job(new_job_data))
                logging.debug("Receive successful, found " + str(new_job_data))
            else:
                self.received.append(self.recv(source=MPI.ANY_SOURCE))
            for job in self.received:
                self.add_job(job)
        del self.received[:]

    def send_back(self, job):
        """
        Send the job back to the node who asked for the computation
        to be done.
        """
        logging.info("Machine " + str(self.rank) + " is sending back " + str(job.game_state.pos) + " to " + str(job.parent))
        resolve_job = Job(Job.RESOLVE, job.game_state, job.parent, job.job_id)
        if NP:
            resolve_job_to_send = resolve_job.construct_numpy_representation()
            logging.debug("Preparing to send back " + str(resolve_job_to_send))
            self.send(resolve_job_to_send, dest=resolve_job.parent)
            logging.debug("Send successful")
        else:
            self.send(resolve_job, dest=resolve_job.parent)

    def _res_red(self, res1, res2):
        """
        Private method that helps reduce in resolve.
        """
        nums = {WIN : 0, DRAW : 1, TIE : 2, LOSS : 3}
        states = (WIN, DRAW, TIE, LOSS)

        if res2 == None:
            return negate(res1)
        max_num = max(nums[res1], nums[res2])
        return negate(states[max_num])


    def _remote_red(self, rem1, rem2):
        """
        Private method that helps reduce remoteness
        Takes in two GameStates, and returns a Job with
        with an appropriate remoteness.
        """
        # TODO: Make cleaner.
        if rem2 is None:
            return GameState(None, rem1.remoteness, rem1.state)

        if rem1.state == LOSS or rem2.state == LOSS:
            if rem1.state == LOSS and rem2.state == WIN:
                return GameState(None, rem1.remoteness, LOSS)
            elif rem1.state == WIN and rem2.state == LOSS:
                return GameState(None, rem2.remoteness, LOSS)
            else:
                return GameState(None, min(rem1.remoteness, rem2.remoteness), LOSS)
        elif rem2.state == WIN and rem1.state == WIN:
            return GameState(None, max(rem1.remoteness, rem2.remoteness), WIN)
        else:
            # Use rem1.state by default, but rem2.state should work too.
            return GameState(None, max(rem1.remoteness, rem2.remoteness), rem1.state)

    def reduce_helper(self, function, data):
        if len(data) == 1:
            return function(data[0], None)
        return reduce(function, data)

    def resolve(self, job):
        """
        Given a list of WIN, LOSS, TIE, (DRAW, well maybe for later)
        determine whether this position in the game tree is a WIN,
        LOSS, TIE, or DRAW.
        """
        self._counter[job.job_id] -= 1
        self._pending[job.job_id].append(job.game_state) # [Job, GameState, ... ]
        if self._counter[job.job_id] == 0: # Resolve _pending.
            to_resolve = self._pending[job.job_id][0] # Job
            if to_resolve.game_state.is_primitive():
                self.resolved[to_resolve.game_state.index] = to_resolve.game_state.primitive
                self.remote[to_resolve.game_state.index] = 0
                job.game_state.state = self.resolved[to_resolve.game_state.index]
                job.game_state.remoteness = self.remote[to_resolve.game_state.index]
            else:
                resolve_data = list(self._pending[job.job_id][1:]) # [GameState, GameState, ...]
                if __debug__:
                    res_str = "Resolve data:"
                    for state in resolve_data:
                        res_str = res_str + " " + str(state.pos) + "/" + str(state.state) + "/" + str(state.remoteness)
                    logging.info(res_str)
                state_red = [gs.state for gs in resolve_data]
                #remoteness_red = [gs.remoteness for gs in resolve_data]
                self.resolved[to_resolve.game_state.index] = self.reduce_helper(self._res_red, state_red)
                self.remote[to_resolve.game_state.index] = self.reduce_helper(self._remote_red, resolve_data).remoteness + 1
                job.game_state.state = self.resolved[to_resolve.game_state.index]
                job.game_state.remoteness = self.remote[to_resolve.game_state.index]
            logging.info("Resolved " + str(job.game_state.pos) +
                         " to " + STATE_MAP[job.game_state.state] +
                         ", remoteness: " + str(self.remote[to_resolve.game_state.index]))
            to = Job(Job.SEND_BACK, job.game_state, to_resolve.parent, to_resolve.job_id)
            self.add_job(to)
        self.stats_dict["resolve_data"] = self.resolved
