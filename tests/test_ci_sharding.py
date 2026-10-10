from conftest import _partition_collected_items


def test_ci_shards_partition_the_collected_suite_once(request):
    items = getattr(request.config, "_ci_original_items", request.session.items)
    nodeids = [item.nodeid for item in items]
    shards = _partition_collected_items(items, 2)

    assert len(set(nodeids)) == len(nodeids)
    assert sorted(item.nodeid for shard in shards for item in shard) == sorted(nodeids)
    assert not ({item.nodeid for item in shards[0]} & {item.nodeid for item in shards[1]})
    assert shards == _partition_collected_items(items, 2)

    file_shards = {}
    for index, shard in enumerate(shards):
        for item in shard:
            path = str(item.path)
            assert path not in file_shards or file_shards[path] == index
            file_shards[path] = index

    weights = [len(shard) for shard in shards]
    largest_file = max(sum(str(item.path) == path for item in items) for path in file_shards)
    assert abs(weights[0] - weights[1]) <= largest_file
