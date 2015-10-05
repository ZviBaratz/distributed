from contextlib import contextmanager
from multiprocessing import Process
from operator import add, mul
from time import time
from toolz import merge

import dask
from dask.core import get_deps
from distributed3 import Center, Worker
from distributed3.utils import ignoring
from distributed3.client import gather_from_center
from distributed3.dask import _get, _get2, rewind

from tornado import gen
from tornado.ioloop import IOLoop


def inc(x):
    return x + 1


def _test_cluster(f):
    @gen.coroutine
    def g(get):
        c = Center('127.0.0.1', 8017)
        c.listen(c.port)
        a = Worker('127.0.0.1', 8018, c.ip, c.port, ncores=1)
        yield a._start()
        b = Worker('127.0.0.1', 8019, c.ip, c.port, ncores=1)
        yield b._start()

        while len(c.ncores) < 2:
            yield gen.sleep(0.01)

        try:
            yield f(c, a, b, get)
        finally:
            with ignoring():
                yield a._close()
            with ignoring():
                yield b._close()
            c.stop()

    for get in [_get, _get2]:
        IOLoop.current().run_sync(lambda: g(get))


def test_scheduler():
    dsk = {'x': 1, 'y': (add, 'x', 10), 'z': (add, (inc, 'y'), 20),
           'a': 1, 'b': (mul, 'a', 10), 'c': (mul, 'b', 20),
           'total': (add, 'c', 'z')}
    keys = ['total', 'c', ['z']]

    @gen.coroutine
    def f(c, a, b, get):
        result = yield get(c.ip, c.port, dsk, keys)
        result2 = yield gather_from_center((c.ip, c.port), result)

        expected = dask.async.get_sync(dsk, keys)
        assert tuple(result2) == expected
        assert set(a.data) | set(b.data) == {'total', 'c', 'z'}

    _test_cluster(f)


def test_scheduler_errors():
    def mydiv(x, y):
        return x / y
    dsk = {'x': 1, 'y': (mydiv, 'x', 0)}
    keys = 'y'

    @gen.coroutine
    def f(c, a, b, get):
        try:
            result = yield get(c.ip, c.port, dsk, keys)
            assert False
        except ZeroDivisionError as e:
            # assert 'mydiv' in str(e)
            pass

    _test_cluster(f)


def test_gather():
    dsk = {'x': 1, 'y': (inc, 'x')}
    keys = 'y'

    @gen.coroutine
    def f(c, a, b, get):
        result = yield get(c.ip, c.port, dsk, keys, gather=True)
        assert result == 2

    _test_cluster(f)



def test_rewind():
    """
        alpha  beta
          |     |
          x     y
         / \   / \ .
        a    b    c     d
        |    |    |     |
        A    B    C     D

    We have x and C, we lose b and D.  We'll need to recompute D, B and b.
    """
    dsk = {'A': 1, 'B': 2, 'C': 3, 'D': 4,
           'a': (inc, 'A'), 'b': (inc, 'B'), 'c': (inc, 'C'),
           'x': (add, 'a', 'b'), 'y': (add, 'b', 'c'),
           'alpha': (inc, 'x'), 'beta': (inc, 'y'), 'd': (inc, 'D')}
    dependencies, dependents = get_deps(dsk)
    waiting = {'alpha': {'x'}, 'beta': 'y',
               'y': {'c'}}
    waiting_data = {'x': {'alpha'}, 'y': {'beta'},
                    'b': {'y'}, 'C': {'c'}}  # why is C here and not above?
    has_what = {'alice': {'x'}, 'bob': {'C'}}
    who_has = {'x': {'alice'}, 'C': {'bob'}}
    stacks = {'alice': ['alpha'], 'bob': ['c']}
    finished_results = {'d'}

    result = rewind(dependencies, dependents, waiting, waiting_data,
                    finished_results, stacks, who_has, 'b')

    e_waiting = {'alpha': {'x'}, 'beta': 'y',
                'y': {'b', 'c'},
                'b': {'B'}}
    e_waiting_data = {'x': {'alpha'}, 'y': {'beta'},
                    'b': {'y'},
                    'B': {'b'}, 'C': {'c'}}

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert result == {'B': 'alice'} or result == {'B': 'bob'}

    result = rewind(dependencies, dependents, waiting, waiting_data,
                    finished_results, stacks, who_has, 'd')

    e_waiting = {'alpha': {'x'}, 'beta': 'y',
                'y': {'b', 'c'},
                'b': {'B'}, 'd': {'D'}}
    e_waiting_data = {'x': {'alpha'}, 'y': {'beta'},
                    'b': {'y'},
                    'B': {'b'}, 'C': {'c'}, 'D': {'d'},
                    'd': set()}

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert finished_results == set()
    assert result == {'D': 'alice'} or result == {'D': 'bob'}


    """  Upon losing b we need to add it back into waiting_data for a
        b   c
         \ /
          a
    """
    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'a')}
    dependencies, dependents = get_deps(dsk)
    waiting = {}
    waiting_data = {'a': {'c'}, 'b': set(), 'c': set()}
    stacks = {'bob': ['c']}
    who_has = {'c': {'bob'}, 'a': {'bob'}}
    has_what = {'bob': {'a', 'c'}}
    finished_results = {'b'}

    result = rewind(dependencies, dependents, waiting, waiting_data,
                    finished_results, stacks, who_has, 'b')

    assert waiting_data == {'a': {'b', 'c'}, 'b': set(), 'c': set()}
    assert waiting == {}
    assert set(stacks['bob']) == {'b', 'c'}

    assert result == {'b': 'bob'}


