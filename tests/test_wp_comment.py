"""Tests für die Validierung von wp_comment_list/wp_comment_moderate (ohne Serverzugriff)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def test_ids_ok_and_dedup():
    assert server._wp_comment_ids([295, 3, 295]) == [295, 3]
    assert server._wp_comment_ids(7) == [7]


@pytest.mark.parametrize("ids", [[], [0], [-1], ["295"], [True], list(range(1, 202))])
def test_ids_rejected(ids):
    with pytest.raises(ValueError):
        server._wp_comment_ids(ids)


def test_counts_parse():
    out = "approved:        12\nspam:            3\ntrash:           0\npost-trashed:    0\nall:             15\nmoderated:       1\ntotal_comments:  15\n"
    c = server._wp_comment_counts(out)
    assert c["approved"] == 12 and c["moderated"] == 1 and c["total_comments"] == 15


def test_view_shortens_content():
    c = {"comment_ID": "295", "comment_post_ID": "10", "comment_approved": "0",
         "comment_author": "x", "comment_content": "a  b\n" * 200}
    v = server._wp_comment_view(c)
    assert v["id"] == 295 and v["status"] == "0" and v["inhalt"].endswith("…")
    assert len(v["inhalt"]) == 301


def test_action_and_status_validation():
    with pytest.raises(ValueError, match="action"):
        server.wp_comment_moderate("example.com", "httpdocs", [1], action="nuke")
    with pytest.raises(ValueError, match="status"):
        server.wp_comment_list("example.com", "httpdocs", status="pending")
    with pytest.raises(ValueError, match="limit"):
        server.wp_comment_list("example.com", "httpdocs", limit=0)


def test_delete_is_only_action_with_force():
    assert server._WP_COMMENT_ACTIONS["delete"][-1] == "--force"
    assert all("--force" not in v for k, v in server._WP_COMMENT_ACTIONS.items() if k != "delete")
