"""Part 5b, no database: the agent's view → yes/no suggestions; nothing changes your lists by itself."""

from app.modules.m2_jobs.reader import candidate_brief, profile_brief
from app.modules.m5_profile import direction as D
from tests.test_m5_profile import STUB  # noqa: F401  (same sample profile)

CUR = {"progression": ["business analytics", "product analytics"], "lateral": [],
       "excluded": ["risk analytics", "pure data science / ML"], "target_roles": ["Senior Data Analyst", "Analytics Consulting"],
       "lateral_appetite": "medium", "track": "individual_contributor", "open_to_management_track": False}


def fam(family, verdict, strength="strong", why="w"):
    return D.FamilyView(family=family, verdict=verdict, condition="", why=why, strength=strength)


def view(**kw):
    base = dict(position="p", trajectory="t", lateral_appetite="medium", appetite_why="", track="individual_contributor",
                open_to_management=False, track_why="",
                families=[fam("Business Analytics", "progression"), fam("product analytics", "progression")],
                target_roles=[D.TitleView(title="Senior Data Analyst", action="keep", why="", strength="strong"),
                              D.TitleView(title="Analytics Consulting", action="keep", why="", strength="strong")])
    return D.DirectionView(**{**base, **kw})


def kinds(v):
    return sorted((s["kind"], s["target"], s["item"]) for s in D.suggestions_from(v, CUR))


def test_agreeing_view_means_no_questions():
    assert kinds(view()) == []                          # same families (case doesn't matter): nothing to ask


def test_new_family_and_title_are_asked_not_added():
    v = view(families=[*view().families, fam("forward deployed engineering", "progression")],
             target_roles=[*view().target_roles,
                           D.TitleView(title="Forward Deployed Engineer", action="add", why="AI agents", strength="strong")])
    assert kinds(v) == [("add", "progression", "forward deployed engineering"),
                        ("add", "target_roles", "Forward Deployed Engineer")]


def test_including_an_excluded_family_and_dropping_a_title_are_asked():
    v = view(families=[*view().families, fam("pure data science / ML", "progression")],
             target_roles=[D.TitleView(title="Senior Data Analyst", action="drop", why="below your level", strength="strong"),
                           D.TitleView(title="Analytics Consulting", action="keep", why="", strength="strong")])
    assert kinds(v) == [("include", "progression", "pure data science / ML"), ("remove", "target_roles", "Senior Data Analyst")]


def test_appetite_and_track_changes_are_asked():
    v = view(lateral_appetite="high", track="people_manager", open_to_management=True)
    assert kinds(v) == [("appetite", "lateral_appetite", "high"), ("track", "track", "people_manager (open to managing)")]


def test_drafts_dont_see_your_families():
    from app.config_schemas import OwnerStub

    o = OwnerStub.model_validate({**STUB, "role_families": {"progression": ["x"], "lateral": [], "excluded": ["y"]}})
    assert "Excluded families" in candidate_brief(o) and "Excluded families" not in profile_brief(o)
    assert "individual contributor" in candidate_brief(o) and "individual contributor" not in profile_brief(o)
