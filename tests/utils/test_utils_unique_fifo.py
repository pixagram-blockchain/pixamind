# pylint: disable=missing-docstring, invalid-name
from hive.utils.unique_fifo import UniqueFIFO


def test_unique_queue():
    q = UniqueFIFO()
    assert q.extend(set(['tim', 'bob'])) == 2
    assert len(q) == 2

    assert q.extend(set(['tim', 'foo'])) == 1
    assert len(q) == 3

    # extend() takes a set, so insertion order is the set's iteration order:
    # assert on membership and counts, not on positions.
    pop1 = q.shift_portion(3)
    assert len(pop1) == 1 and pop1[0] in {'tim', 'bob'}
    assert len(q) == 2

    assert q.extend(set()) == 0
    assert len(q) == 2

    assert q.extend(set(['foo'])) == 0
    assert len(q) == 2

    pop2 = q.shift_portion(1)
    assert set(pop1 + pop2) == {'tim', 'bob', 'foo'}
    assert not q

    assert q.shift_portion(500) == []

    assert q.extend(set(['tim', 'bob'])) == 2
    assert q.extend(set(['tim', 'tom'])) == 1
    assert q.extend(set(['tim', 'foo'])) == 1

    first_two = q.shift_count(2)
    assert set(first_two) == set(['tim', 'bob'])
    assert q.shift_count(1) == ['tom']
    assert q.shift_count(400) == ['foo']
    assert q.shift_count(400) == []

    q.extend(set(['foo', 'bar']))
    q.add('foo')
    q.add('cat')
    assert len(q) == 3
    pop3 = q.shift_portion(1)
    assert set(pop3[:2]) == {'foo', 'bar'} and pop3[2] == 'cat'
