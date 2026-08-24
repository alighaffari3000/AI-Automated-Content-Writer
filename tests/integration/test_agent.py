"""Structural checks on the pipeline graph.

No model is called here. These catch the mistakes that would otherwise only
show up mid-run: a node nobody reaches, a route with no edge, a reviewer whose
name the gate does not recognise.
"""

from __future__ import annotations

from app.agent import REVIEWER_NODES, root_agent
from app.rules import SEO_REVIEWER


def node_names() -> set[str]:
    return {node.name for node in root_agent.graph.nodes}


def test_every_stage_is_present():
    names = node_names()
    for expected in (
        "load_run_context",
        "topic_planner",
        "open_article",
        "researcher",
        "fact_builder",
        "persist_registry",
        "writer",
        "join_reviews",
        "evaluate_gate",
        "judge",
        "finalize",
        "abort_run",
        *REVIEWER_NODES,
    ):
        assert expected in names, f"{expected} is missing from the graph"


def test_the_three_reviewers_run_in_parallel_off_the_writer():
    outgoing = {
        edge.to_node.name
        for edge in root_agent.graph.edges
        if edge.from_node.name == "writer"
    }
    assert outgoing == set(REVIEWER_NODES)


def test_the_gate_can_only_revise_or_finish():
    routes = {
        edge.route: edge.to_node.name
        for edge in root_agent.graph.edges
        if edge.from_node.name == "evaluate_gate"
    }
    assert routes == {"revise": "judge", "finalize": "finalize"}


def test_revision_returns_to_the_writer_through_the_judge():
    """The loop exists, and it goes back through the judge rather than directly."""
    judge_targets = {
        edge.to_node.name
        for edge in root_agent.graph.edges
        if edge.from_node.name == "judge"
    }
    assert judge_targets == {"writer"}


def test_the_subject_is_invented_before_any_research_happens():
    """A person defines the category; the planner decides what to write in it."""
    edges = {
        (edge.from_node.name, edge.to_node.name) for edge in root_agent.graph.edges
    }
    assert ("load_run_context", "topic_planner") in edges
    assert ("topic_planner", "open_article") in edges
    assert ("open_article", "researcher") in edges


def test_the_gate_and_the_reviewer_agree_on_the_seo_reviewer_name():
    """A rename here would silently disable the SEO score check."""
    assert f"{SEO_REVIEWER}_reviewer" in REVIEWER_NODES


def test_nothing_reaches_the_site_without_passing_the_gate():
    inbound = {
        edge.from_node.name
        for edge in root_agent.graph.edges
        if edge.to_node.name == "finalize"
    }
    assert inbound == {"evaluate_gate"}
