"""The checker gets plans in odd shapes from smaller models. It must answer with a rejection the
model can act on, never crash (a crash reaches the model as a bare "AttributeError")."""
import pytest

from src import tools as T
from tests.conftest import SAMPLE_DATE


@pytest.fixture
def pick(prefs):
    prefs()
    return T.scout_meal("dinner", SAMPLE_DATE)["halls"]["East"]["items"][0]


def _plan(**fields):
    return {"date": SAMPLE_DATE, "headline": "x", "message": "m", **fields}


def _dinner(pick, **fields):
    return {"meal": "dinner", "hall": "East", "items": [{"id": pick["id"]}], **fields}


@pytest.mark.parametrize("alternatives", [["Pinto Bean Stew"], "swap for the stew", [42]])
def test_a_swap_written_as_text_is_rejected_with_the_right_shape(pick, alternatives):
    result = T.submit_plan(_plan(meals=[_dinner(pick, alternatives=alternatives)]))
    assert result["status"] == "rejected"
    assert "dinner swap 1" in result["problems"][0] and '"items"' in result["problems"][0]


def test_a_single_swap_object_counts_as_a_list_of_one(pick):
    result = T.submit_plan(_plan(meals=[_dinner(pick, alternatives={"items": [{"id": pick["id"]}]})]))
    assert result["status"] == "accepted" and len(result["plan"]["meals"][0]["alternatives"]) == 1


@pytest.mark.parametrize("meals", ["dinner at East", ["dinner at East"], {"dinner": {"hall": "East"}}])
def test_meals_in_the_wrong_shape_are_rejected_not_crashed(pick, meals):
    result = T.submit_plan(_plan(meals=meals))
    assert result["status"] == "rejected"
    assert '"meal"' in result["problems"][0] and '"hall"' in result["problems"][0]


def test_items_given_as_one_id_string_mean_that_one_item(pick):
    result = T.submit_plan(_plan(meals=[{"meal": "dinner", "hall": "East", "items": pick["id"]}]))
    assert result["status"] == "accepted"
    assert [i["id"] for i in result["plan"]["meals"][0]["items"]] == [pick["id"]]


def test_tips_and_adaptations_always_come_out_as_lists_of_text(pick):
    result = T.submit_plan(_plan(meals=[_dinner(pick)], tips="Go early.", adaptations=["Moved dinner", 3]))
    plan = result["plan"]
    assert plan["tips"] == ["Go early."] and plan["adaptations"] == ["Moved dinner", "3"]
