"""Host pairs for SuperBench ib-traffic (RDMA point to point, same rail on both ends).

ib-traffic reads a pairing file (--config): one line per round, "i,j;k,l" pairs that run at the same
time, where i is the line of the host in SuperBench's hostfile. SuperBench writes that hostfile from
the hosts' short OS hostnames (Ansible "ansible_hostname"), sorted case-insensitively, so the indexes
are computed from the hostnames read on the hosts, not from cluster.yaml order.

Pairings (nodes in mtest rank order: rack, then tray):
  rack_adjacent  inside each rack: tray 1-2, 3-4, ...; all racks in the same round
  rack_half      inside each rack: tray i with tray i + n/2
  cross_rack     rack A tray i with rack B tray i; racks are paired round-robin (one round per rack pairing)
"""

from mtest.util import natural_key

PAIRINGS = ("round_robin", "rack_adjacent", "rack_half", "cross_rack")


def _by_rack(nodes):
    racks = {}
    for n in nodes:
        racks.setdefault(n.host.rack, []).append(n)
    return [racks[r] for r in sorted(racks, key=natural_key)]


def rack_pairs(nodes, mode="rack_adjacent"):
    """(rounds, notes). rounds: list of [(node, node)]. An odd host pairs with the first one in a second round."""
    first, second, notes = [], [], []
    for members in _by_rack(nodes):
        n = len(members)
        if n < 2:
            notes.append("rack {}: only {} host, no pair".format(members[0].host.rack, n))
            continue
        if mode == "rack_half":
            half = n // 2
            first += [(members[i], members[i + half]) for i in range(half)]
        else:
            first += [(members[i], members[i + 1]) for i in range(0, n - 1, 2)]
        if n % 2:
            second.append((members[-1], members[0]))
            notes.append("rack {}: {} hosts, {} paired again with {} in a second round".format(
                members[0].host.rack, n, members[-1].host.name, members[0].host.name))
    return [r for r in (first, second) if r], notes


def _rack_rounds(count):
    """Round-robin pairings of racks 0..count-1 (circle method); each round pairs every rack at most once."""
    ids = list(range(count)) + ([None] if count % 2 else [])
    rounds = []
    for _ in range(len(ids) - 1):
        half = len(ids) // 2
        rounds.append([(ids[i], ids[-1 - i]) for i in range(half) if ids[i] is not None and ids[-1 - i] is not None])
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds


def cross_rack_pairs(nodes, all_rounds=True):
    """(rounds, notes): rack A tray t with rack B tray t, for every rack pairing (or only the first round)."""
    racks = _by_rack(nodes)
    notes = []
    if len(racks) < 2:
        return [], ["cross_rack needs hosts of at least 2 racks"]
    rounds = []
    plans = _rack_rounds(len(racks))
    for plan in plans if all_rounds else plans[:1]:
        pairs = []
        for a, b in plan:
            ta = {n.host.tray: n for n in racks[a]}
            tb = {n.host.tray: n for n in racks[b]}
            missing = sorted(set(ta) ^ set(tb))
            if missing:
                notes.append("{} / {}: trays {} have no peer".format(racks[a][0].host.rack, racks[b][0].host.rack,
                                                                    ", ".join(map(str, missing))))
            pairs += [(ta[t], tb[t]) for t in sorted(set(ta) & set(tb))]
        if pairs:
            rounds.append(pairs)
    return rounds, notes


def plan(nodes, mode, all_rounds=True):
    if mode in ("rack_adjacent", "rack_half"):
        return rack_pairs(nodes, mode)
    if mode == "cross_rack":
        return cross_rack_pairs(nodes, all_rounds)
    raise ValueError("unknown ib pairing {}".format(mode))


def sb_order(os_names):
    """Hostnames in SuperBench hostfile order (Jinja2 sort: case-insensitive)."""
    return sorted(os_names, key=lambda s: s.lower())


def config_text(rounds, os_name):
    """ib-traffic pairing file. os_name: mtest host name -> short OS hostname of that host."""
    order = sb_order(sorted(set(os_name.values())))
    idx = {name: i for i, name in enumerate(order)}
    lines = []
    for pairs in rounds:
        lines.append(";".join("{},{}".format(idx[os_name[a.host.name]], idx[os_name[b.host.name]]) for a, b in pairs))
    return "\n".join(lines) + "\n"
