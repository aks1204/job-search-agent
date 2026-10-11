from app.modules.m1_companies.scouts.aggregators import AdzunaScout, JoobleScout, RemotiveScout
from app.modules.m1_companies.scouts.base import Scout, ScoutContext
from app.modules.m1_companies.scouts.hn import HNScout
from app.modules.m1_companies.scouts.packs import PacksScout
from app.modules.m1_companies.scouts.peers import PeersScout
from app.modules.m1_companies.scouts.seeds import SeedScout
from app.modules.m1_companies.scouts.waas import WaaSScout
from app.modules.m1_companies.scouts.yc import YCScout

SCOUTS: dict[str, type[Scout]] = {
    cls.name: cls
    for cls in (SeedScout, PacksScout, PeersScout, YCScout, WaaSScout, AdzunaScout, JoobleScout, RemotiveScout, HNScout)
}

__all__ = ["SCOUTS", "Scout", "ScoutContext"]
