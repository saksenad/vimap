# -*- coding: utf-8 -*-
'''
Provides an interface for defining worker processes.
'''
from __future__ import absolute_import
from __future__ import print_function

import mock
import os
import stat
import testify as T
import vimap.pool
import vimap.queue_manager
import vimap.worker_process
from collections import namedtuple


# decrypt POSIX stuff
readable_mode_strings = {
    'directory': stat.S_ISDIR,
    'character_device': stat.S_ISCHR,
    'block_device': stat.S_ISBLK,
    'regular': stat.S_ISREG,
    'fifo': stat.S_ISFIFO,
    'symlink': stat.S_ISLNK,
    'socket': stat.S_ISSOCK}


FDInfo = namedtuple("FDInfo", ["modes", "symlink"])


def fd_type_if_open(fd_number):
    """For a given open file descriptor, return a list of human-readable
    strings describing the file type.
    """
    fd_stat = os.fstat(fd_number)
    return FDInfo(
        modes=[k for k, v in readable_mode_strings.items() if v(fd_stat.st_mode)],
        symlink=os.readlink("/proc/{0}/fd/{1}".format(os.getpid(), fd_number)))


def get_open_fds(retries=3):
    """
    Returns a map,

        fd (int) --> FDInfo
    """
    unix_fd_dir = "/proc/{0}/fd".format(os.getpid())
    fds = [(int(i), os.path.join(unix_fd_dir, i)) for i in os.listdir(unix_fd_dir)]

    try:
        # NOTE: Sometimes, an FD is used to list the above directory. Hence, we should
        # re-check whether the FD still exists (via os.path.exists)
        fds = [i for (i, path) in fds if (i >= 3 and os.path.exists(path))]
        return dict(filter(
            lambda (k, v): v is not None,
            ((i, fd_type_if_open(i)) for i in fds)))
    except OSError:
        if retries == 0:
            raise
        return get_open_fds(retries - 1)


def difference_open_fds(before, after):
    """
    Given two snapshots of open file descriptors, `before` and `after`, returns
    those FDs which were opened (present in `after` but not `before`) and
    closed.
    """
    # "a - b" for dicts -- remove anything in 'a' that has a key in b
    dict_diff = lambda a, b: dict((k, a[k]) for k in (frozenset(a) - frozenset(b)))
    for k in (frozenset(after) & frozenset(before)):
        if before[k] != after[k]:
            print("WARNING: FD {0} changed from {1} to {2}".format(k, before[k], after[k]))
    return {
        'closed': dict_diff(before, after),
        'opened': dict_diff(after, before)}


class TestOpenFdsMethods(T.TestCase):
    """
    Tests that we can detect open file descriptors.
    """
    def test_open_fds(self):
        first = get_open_fds()
        fd = open('vimap/pool.py', 'r')
        fd2 = open('vimap/pool.py', 'r')
        second = get_open_fds()
        fd.close()
        third = get_open_fds()
        fd2.close()
        T.assert_equal(len(difference_open_fds(first, second)['opened']), 2)
        T.assert_equal(len(difference_open_fds(first, second)['closed']), 0)
        T.assert_equal(len(difference_open_fds(second, third)['closed']), 1)
        T.assert_equal(len(difference_open_fds(second, third)['opened']), 0)


@vimap.worker_process.worker
def basic_worker(xs):
    for x in xs:
        yield x + 1


def repeat(times):
    """Repeats a test to help catch flakiness."""
    def fcn_helper(fcn):
        return lambda *args, **kwargs: [fcn(*args, **kwargs) for _ in xrange(times)]
    return fcn_helper


class TestBasicMapDoesntLeaveAroundFDs(T.TestCase):
    @T.setup_teardown
    def instrument_queue_initiation(self):
        old_init = vimap.queue_manager.VimapQueueManager.__init__
        def instrumented_init(*args, **kwargs):
            self.before_queue_manager_init = get_open_fds()
            old_init(*args, **kwargs)
            self.after_queue_manager_init = get_open_fds()
            self.queue_fds = difference_open_fds(
                self.before_queue_manager_init,
                self.after_queue_manager_init)['opened']
        with mock.patch.object(
                vimap.queue_manager.VimapQueueManager,
                '__init__',
                instrumented_init):
            yield

    @repeat(30)
    def test_all_fds_cleaned_up(self):
        initial_open_fds = get_open_fds()
        pool = vimap.pool.fork_identical(basic_worker, num_workers=1)
        after_fork_open_fds = get_open_fds()
        list(pool.imap([1, 2, 3]).zip_in_out())
        after_finish_open_fds = get_open_fds()

        # Check that some FDs were opened after forking
        after_fork = difference_open_fds(initial_open_fds, after_fork_open_fds)
        # T.assert_equal(after_fork['closed'], [])
        T.assert_gte(len(after_fork['opened']), 2)  # should have at least 3 open fds
        # All opened files should be FIFOs
        T.assert_equal(all(info.modes == ['fifo'] for info in after_fork['opened'].values()), True)

        after_cleanup = difference_open_fds(after_fork_open_fds, after_finish_open_fds)
        T.assert_gte(len(after_cleanup['closed']), 2)

        left_around = difference_open_fds(initial_open_fds, after_finish_open_fds)
        if len(left_around['opened']) != 0:
            queue_fds_left_around = dict(
                item for item in self.queue_fds.items() if item[0] in left_around['opened'])
            print("Queue FDs left around: {0}".format(queue_fds_left_around))
        T.assert_equal(len(left_around['opened']), 0)


if __name__ == "__main__":
    T.run()
