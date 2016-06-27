# -*- coding: utf-8 -*-
'''
Tests peformance in a system-agnostic manner, by comparing single-threaded
performance with parallel performance.
'''
from __future__ import absolute_import
from __future__ import print_function

import contextlib
import functools
import gc
import logging
import multiprocessing
import resource
import time
import timeit

import mock
import testify as T

import vimap.pool
import vimap.worker_process


@contextlib.contextmanager
def assert_memory_use(name, lower, upper):
    def rss():
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    gc.collect()
    before = rss()
    yield
    gc.collect()
    mem_mb = (rss() - before) / 1024.0
    print(
        "For {0}, memory use increased by {1:.2f} MB".format(name, mem_mb))
    assert lower <= mem_mb, \
        "Memory usage expected to increase by at least {0} MB".format(
            lower)
    assert mem_mb < upper, \
        "Memory usage expected to increase by at most {0} MB".format(upper)


def factorial(n):
    '''dumb implementation of factorial function'''
    result = 1
    for i in xrange(2, n + 1):
        result *= i
    return result


def urandom_stream(size_kb):
    with open("/dev/urandom", "rb") as f:
        for _ in xrange(size_kb):
            yield f.read(1024)


@vimap.worker_process.worker
def factorial_worker(numbers):
    for number in numbers:
        factorial(number)
        yield None  # in case the numbers are large, ignore communication cost


@vimap.worker_process.worker
def simple_sleep_worker(sleep_times):
    for time_to_sleep in sleep_times:
        time.sleep(time_to_sleep)
        yield None


@vimap.worker_process.worker
def string_length_worker(strings):
    for string in strings:
        yield len(string)


def _retry_test(test_fcn):
    """To avoid flakiness inherent in performance tests, we allow the test
    function to fail once, so long as it succeeds a second time.
    """

    @functools.wraps(test_fcn)
    def inner(*args, **kwargs):
        try:
            return test_fcn(*args, **kwargs)
        except Exception as e:
            logging.warning(
                "Warning: performance test {0} "
                "failed with exception {0}: {1}, retrying"
                .format(test_fcn.__name__, type(e), e))
            return test_fcn(*args, **kwargs)

    return inner


@T.suite("performance",
         "These tests may flake if your testing server is overloaded.")
class PerformanceTest(T.TestCase):
    def get_speedup_factor(self, baseline_fcn, optimized_fcn, num_tests):
        baseline_performance = timeit.timeit(baseline_fcn,
                                             number=num_tests)
        optimized_performance = timeit.timeit(optimized_fcn,
                                              number=num_tests)
        _message = "Performance test too fast, susceptible to overhead"
        T.assert_gt(baseline_performance, 0.005, _message)
        T.assert_gt(optimized_performance, 0.005, _message)
        return (baseline_performance / optimized_performance)

    def test_retry_raises_on_second_failure(self):
        """Dumb paranoia test to check our _retry_test decorator."""

        @_retry_test
        def always_fails():
            raise ValueError()

        with T.assert_raises(ValueError):
            with mock.patch.object(logging,
                                   'warning'):  # suppress console spam
                always_fails()

    @_retry_test
    def test_performance(self):
        # NOTE: Avoid hyperthreading, which doesn't help performance
        # in our test case.
        num_workers = min(4, multiprocessing.cpu_count() / 2)
        T.assert_gt(num_workers, 1,
                    "Too few cores to run performance test.")

        start = 15000
        num_inputs = 2 * num_workers
        inputs = tuple(xrange(start, start + num_inputs))

        def factor_sequential():
            for i in inputs:
                factorial(i)

        pool = vimap.pool.fork_identical(factorial_worker,
                                         num_workers=num_workers)

        def factor_parallel():
            pool.imap(inputs).block_ignore_output(close_if_done=False)

        speedup_ratio = self.get_speedup_factor(factor_sequential,
                                                factor_parallel, 4)
        efficiency = speedup_ratio / num_workers
        print(
            "Easy performance test efficiency: "
            "{0:.1f}% ({1:.1f}x speedup)".format(
                efficiency * 100., speedup_ratio))
        T.assert_gt(efficiency, 0.65, "Failed performance test!!")

    @_retry_test
    def test_chunking_really_is_faster(self):
        """Chunking should be faster when the tasks are really small (so queue
        communication overhead is the biggest factor).
        """
        inputs = tuple(xrange(1, 10)) * 1000
        normal_pool = vimap.pool.fork_identical(factorial_worker,
                                                num_workers=2)
        chunked_pool = vimap.pool.fork_identical_chunked(factorial_worker,
                                                         num_workers=2)

        def factor_normal():
            normal_pool.imap(inputs).block_ignore_output(
                close_if_done=False)

        def factor_chunked():
            chunked_pool.imap(inputs).block_ignore_output(
                close_if_done=False)

        speedup_ratio = self.get_speedup_factor(factor_normal,
                                                factor_chunked,
                                                2)
        print(
            "Chunked performance test: {0:.1f}x speedup".format(
                speedup_ratio))
        T.assert_gt(speedup_ratio, 5)

    def run_big_fork_test(self, time_sleep_s, num_workers, num_inputs,
                          num_test_iterations):
        """Common setup for the big fork test; see usage in the two tests below.

        :returns: The amount of time the test took, in seconds.
        """
        pool = vimap.pool.fork_identical(simple_sleep_worker,
                                         num_workers=num_workers)

        def sleep_in_parallel():
            pool.imap(time_sleep_s for _ in xrange(num_inputs))
            pool.block_ignore_output(close_if_done=False)

        return timeit.timeit(sleep_in_parallel,
                             number=num_test_iterations) / num_test_iterations

    @_retry_test
    def test_big_fork(self):
        """Tests that we can fork a large number of processes, each of which
        will wait for a few milliseconds, and return.

        NOTE: currently fails if you bump 70 up to 200.
        We're going to fix this very soon.
        """
        time_sleep_s = 0.2
        test_time = self.run_big_fork_test(time_sleep_s, 70, 70, 3)
        print(
            "Big fork performance test: {0:.2f} s (nominal: {1:.2f} s)".format(
                test_time, time_sleep_s))
        T.assert_lt(test_time, time_sleep_s * 2)

    def test_big_fork_test(self):
        """Tests that if we have one more input, the big fork performance test
        would fail. This makes sure the above test is really doing something.
        """
        time_sleep_s = 0.2
        test_time = self.run_big_fork_test(time_sleep_s, 70, 71, 1)
        T.assert_gt(test_time, time_sleep_s * 2)

    def test_memory_consumption_reading_input(self):
        """Tests that memory usage increases when expected."""
        with assert_memory_use(
                "test-should-increase-read-urandom-streaming",
                0, 0.5):
            for _ in urandom_stream(size_kb=2048):
                pass

        with assert_memory_use("test-should-increase-read-urandom", 0.5,
                               11):
            buf = list(urandom_stream(size_kb=10 * 1024))
        buf.pop(0)

    def test_memory_consumption_stream_processing(self):
        """Tests that memory usage is constant when
        processing a large stream."""
        pool = vimap.pool.fork_identical(string_length_worker)

        with assert_memory_use("test-stream-40-mb", 0, 3):
            pool.imap(
                urandom_stream(size_kb=40 * 1024)).block_ignore_output()
