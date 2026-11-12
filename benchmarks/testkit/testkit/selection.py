"""Host selection and grouping for test items."""

from testkit.items import matches
from testkit.util import natural_key


class SelectionError(ValueError):
    pass


class Group:
    """Hosts that run one item together. hosts are in rank order: rack, then tray."""

    def __init__(self, id, hosts, index):
        self.id = id
        self.hosts = hosts
        self.index = index

    @property
    def master(self):
        return self.hosts[0]

    def __repr__(self):
        return "Group({}, {} hosts)".format(self.id, len(self.hosts))


def _tokens(selector):
    if selector is None:
        return ["all"]
    if isinstance(selector, str):
        return [t.strip() for t in selector.split(",") if t.strip()]
    if isinstance(selector, list):
        out = []
        for part in selector:
            if isinstance(part, list):
                raise SelectionError("nested list in host selector: {}".format(part))
            out.extend(_tokens(str(part)))
        return out
    raise SelectionError("host selector must be a string or a list, got {!r}".format(selector))


def resolve(cluster, selector):
    """Return hosts matched by a selector, in rank order. Raises SelectionError on unknown names."""
    racks = set(cluster.racks)
    tags = set(cluster.tags)
    picked = set()
    for token in _tokens(selector):
        if token == "all":
            picked.update(h.name for h in cluster.hosts)
        elif token.startswith("tag:"):
            tag = token[4:]
            if tag not in tags:
                raise SelectionError("no host has tag '{}'".format(tag))
            picked.update(h.name for h in cluster.hosts if tag in h.tags)
        elif token in racks:
            picked.update(h.name for h in cluster.rack_hosts(token))
        elif token in cluster.by_name:
            picked.add(token)
        else:
            raise SelectionError("'{}' is not a host, rack, tag:<name> or all".format(token))
    return [h for h in cluster.hosts if h.name in picked]


def skipped(host, item_id):
    return any(matches(item_id, s) for s in host.skip)


def _group_id(hosts, cluster, fallback):
    racks = sorted({h.rack for h in hosts}, key=natural_key)
    full = all(len(cluster.rack_hosts(r)) == sum(1 for h in hosts if h.rack == r) for r in racks)
    return "+".join(racks) if full else fallback


def build_groups(cluster, item, cfg, only=None, exclude=None, drop=None):
    """Split the item's hosts into groups according to its scope.

    only/exclude come from the command line and can only narrow the selection; drop is a
    set of host names removed by testkit itself (for example after an earlier failure).
    Returns (groups, notes) where notes explain hosts that were dropped.
    """
    notes = []
    scope = cfg.get("scope", "node")
    allowed = resolve(cluster, cfg.get("hosts", "all"))
    if only is not None:
        keep = {h.name for h in resolve(cluster, only)}
        allowed = [h for h in allowed if h.name in keep]
    drop = set(drop or ())
    if exclude:
        drop |= {h.name for h in resolve(cluster, exclude)}
    for h in allowed:
        if skipped(h, item.id):
            notes.append("{} skips {} (cluster.yaml skip)".format(h.name, item.id))
            drop.add(h.name)
    allowed = [h for h in allowed if h.name not in drop]

    if scope == "node":
        groups = [[h] for h in allowed]
        ids = [g[0].name for g in groups]
    elif scope == "rack":
        racks = sorted({h.rack for h in allowed}, key=natural_key)
        groups = [[h for h in allowed if h.rack == r] for r in racks]
        ids = racks
    elif scope == "group":
        spec = cfg.get("groups")
        if spec:
            if not isinstance(spec, list) or not all(isinstance(g, (list, str)) for g in spec):
                raise SelectionError("groups must be a list of host selectors, e.g. [[R1, R2], [R3, R4]]")
            names = {h.name for h in allowed}
            groups = [[h for h in resolve(cluster, g) if h.name in names] for g in spec]
        else:
            groups = [allowed]
        ids = [_group_id(g, cluster, "g{}".format(i + 1)) if g else "g{}".format(i + 1)
               for i, g in enumerate(groups)]
    else:
        raise SelectionError("scope must be node, rack or group, got '{}'".format(scope))

    out = []
    for gid, hosts in zip(ids, groups):
        if not hosts:
            notes.append("group {} has no hosts left and is dropped".format(gid))
            continue
        out.append(Group(gid, hosts, len(out)))
    return out, notes
