"""Replies and systems: the user's answers and what they learn from their outreach."""

import system_stats
from test_app import AUTH, clock, client, env, hit, new_id, register  # noqa: F401 (fixtures)


def reply(client, eid, sender, age=0):
    return client.post(f"/api/emails/{eid}/reply", json={"from": sender, "age": age}, headers=AUTH)


def test_a_reply_from_a_recipient_counts(client, clock):
    eid = new_id()
    register(client, eid, recipients=["Bob <Bob@x.com>"])
    clock["now"] += 3600
    r = reply(client, eid, "bob@X.com", age=600)
    assert r.status_code == 200 and r.json()["replied_at"] == clock["now"] - 600
    e = client.get(f"/api/emails/{eid}", headers=AUTH).json()
    assert e["replied_at"] == clock["now"] - 600 and e["status"] == "not_opened"


def test_a_reply_from_anyone_else_is_ignored(client, clock):
    eid = new_id()
    register(client, eid, recipients=["bob@x.com"])
    assert reply(client, eid, "colleague@x.com").json()["replied_at"] is None
    assert reply(client, new_id(), "bob@x.com").status_code == 404


def test_the_earliest_reply_is_kept_and_never_before_the_send(client, clock):
    eid = new_id()
    register(client, eid, recipients=["bob@x.com"])
    sent = clock["now"]
    clock["now"] += 7200
    reply(client, eid, "bob@x.com", age=60)
    reply(client, eid, "bob@x.com", age=3600)
    assert reply(client, eid, "bob@x.com", age=0).json()["replied_at"] == clock["now"] - 3600
    other = new_id()
    register(client, other, recipients=["bob@x.com"])
    assert reply(client, other, "bob@x.com", age=86400).json()["replied_at"] == clock["now"]
    assert sent < clock["now"]


def test_mark_replied_by_hand_and_undo_keeps_the_campaign(client, clock):
    eid = new_id()
    register(client, eid, campaign="Launch")
    clock["now"] += 60
    e = client.patch(f"/api/emails/{eid}", json={"replied": True}, headers=AUTH).json()
    assert e["replied_at"] == clock["now"] and e["campaign"] == "Launch"
    e = client.patch(f"/api/emails/{eid}", json={"replied": False}, headers=AUTH).json()
    assert e["replied_at"] is None and e["campaign"] == "Launch"
    e = client.patch(f"/api/emails/{eid}", json={"campaign": None}, headers=AUTH).json()
    assert e["campaign"] is None


def test_replies_in_lists_campaigns_and_stats(client, clock):
    a, b = new_id(), new_id()
    register(client, a, campaign="Launch", recipients=["bob@x.com"])
    register(client, b, campaign="Launch")
    clock["now"] += 100
    reply(client, a, "bob@x.com")
    assert [e["id"] for e in client.get("/api/emails?status=replied", headers=AUTH).json()] == [a]
    camp = client.get("/api/campaigns", headers=AUTH).json()[0]
    assert (camp["replied"], camp["reply_rate"], camp["median_time_to_reply"]) == (1, 0.5, 100)
    stats = client.get("/api/stats", headers=AUTH).json()
    assert (stats["replied"], stats["reply_rate"], stats["emails"]) == (1, 0.5, 2)


def test_a_system_learns_from_its_experiments(client, clock):
    s = client.post("/api/systems", json={"name": "  Game  founders ", "target": 0.4}, headers=AUTH).json()
    assert (s["name"], s["goal"], s["health"], s["current"]) == ("Game founders", "reply_rate", "not_measured", None)
    short = client.post(
        f"/api/systems/{s['id']}/experiments", json={"idea": "Short subject", "campaign": "Short"}, headers=AUTH
    ).json()
    client.post(f"/api/systems/{s['id']}/experiments", json={"idea": "Name their game", "campaign": "Named"}, headers=AUTH)
    ids = {}
    for camp, n in (("Short", 2), ("Named", 2), ("Elsewhere", 3)):
        ids[camp] = [new_id() for _ in range(n)]
        for eid in ids[camp]:
            register(client, eid, campaign=camp, recipients=["bob@x.com"])
    clock["now"] += 100
    for eid in ids["Named"]:
        reply(client, eid, "bob@x.com")
    hit(client, ids["Short"][0])

    [got] = client.get("/api/systems", headers=AUTH).json()
    assert (got["emails"], got["replied"], got["reply_rate"], got["open_rate"]) == (4, 2, 0.5, 0.25)
    assert (got["current"], got["health"]) == (0.5, "on_target")
    by_idea = {e["idea"]: e for e in got["experiments"]}
    assert by_idea["Name their game"]["reply_rate"] == 1 and by_idea["Short subject"]["opened"] == 1

    e = client.patch(f"/api/experiments/{short['id']}", json={"note": "Too vague"}, headers=AUTH).json()
    assert e["note"] == "Too vague" and e["idea"] == "Short subject"
    e = client.patch(f"/api/experiments/{short['id']}", json={"idea": "Shorter subject"}, headers=AUTH).json()
    assert e["note"] == "Too vague" and e["idea"] == "Shorter subject"

    # A renamed campaign stays linked to its experiment.
    client.patch("/api/campaigns/Short", json={"name": "Short v2"}, headers=AUTH)
    assert client.get(f"/api/systems", headers=AUTH).json()[0]["experiments"][-1]["campaign"] == "Short v2"

    s2 = client.patch(f"/api/systems/{s['id']}", json={"goal": "open_rate", "name": None}, headers=AUTH).json()
    assert (s2["name"], s2["current"], s2["health"]) == ("Game founders", 0.25, "below_target")
    assert client.patch(f"/api/systems/{s['id']}", json={"target": None}, headers=AUTH).json()["health"] == "not_measured"

    assert client.delete(f"/api/systems/{s['id']}", headers=AUTH).json() == {"ok": True}
    assert client.get("/api/systems", headers=AUTH).json() == []
    assert client.patch(f"/api/experiments/{short['id']}", json={"idea": "x"}, headers=AUTH).status_code == 404
    assert len(client.get("/api/emails", headers=AUTH).json()) == 7


def test_system_validation(client):
    assert client.post("/api/systems", json={"name": ""}, headers=AUTH).status_code == 422
    assert client.post("/api/systems", json={"name": "x", "target": 2}, headers=AUTH).status_code == 422
    assert client.post("/api/systems", json={"name": "x", "goal": "clicks"}, headers=AUTH).status_code == 422
    sid = client.post("/api/systems", json={"name": "x"}, headers=AUTH).json()["id"]
    bad = client.post(f"/api/systems/{sid}/experiments", json={"idea": "y", "note": "n" * 5001}, headers=AUTH)
    assert bad.status_code == 422
    assert client.get("/api/systems", headers={"Authorization": "Bearer nope"}).status_code == 401


# ---- categories: what a system varies, and which value wins -------------------



def make_system(client, **kw):
    return client.post("/api/systems", json={"name": "Founders", **kw}, headers=AUTH).json()


def add_category(client, sid, name):
    return client.post(f"/api/systems/{sid}/categories", json={"name": name}, headers=AUTH)


def send(client, campaign, n, replied):
    for i in range(n):
        eid = new_id()
        register(client, eid, campaign=campaign, recipients=["bob@x.com"])
        if i < replied:
            reply(client, eid, "bob@x.com")


def test_statistics_helpers():
    low, high = system_stats.wilson(0, 10)
    assert low == 0 and 0.27 < high < 0.31
    assert system_stats.wilson(0, 0) is None
    assert system_stats.two_proportion_p(30, 100, 10, 100) < 0.001
    assert system_stats.two_proportion_p(5, 10, 5, 10) == 1.0
    assert 190 < system_stats.emails_per_value(0.2, 0.1) < 205
    assert system_stats.emails_per_value(0.2, 0.2) is None


def test_categories_compare_values_and_find_a_clear_winner(client, clock):
    s = make_system(client)
    hook = add_category(client, s["id"], " Hook ").json()
    link = add_category(client, s["id"], "Link").json()
    assert hook["name"] == "Hook" and hook["values"] == []
    assert add_category(client, s["id"], "hook").status_code == 409
    for camp, h, lnk in (("Q1", "Question", "None"), ("C1", "Compliment", "None"), ("Q2", "Question", "Calendly")):
        r = client.post(
            f"/api/systems/{s['id']}/experiments",
            json={"idea": camp, "campaign": camp, "values": {hook["id"]: f" {h} ", link["id"]: lnk}},
            headers=AUTH,
        )
        assert r.json()["values"] == {hook["id"]: h, link["id"]: lnk}
    client.post(f"/api/systems/{s['id']}/experiments", json={"idea": "No hook set", "campaign": "N"}, headers=AUTH)
    send(client, "Q1", 40, 14)
    send(client, "Q2", 40, 12)
    send(client, "C1", 40, 2)
    send(client, "N", 5, 0)

    got = client.get(f"/api/systems/{s['id']}", headers=AUTH).json()
    hook_col, link_col = got["categories"]
    assert [v["value"] for v in hook_col["values"]] == ["Question", "Compliment", None]
    question = hook_col["values"][0]
    assert (question["experiments"], question["emails"], question["replied"], question["winner"]) == (2, 80, 26, True)
    assert question["ci"][0] < 26 / 80 < question["ci"][1]
    assert hook_col["result"]["status"] == "clear" and hook_col["result"]["leader"] == "Question"
    assert hook_col["result"]["p_value"] < 0.05

    # Calendly 12/40 against None 16/80: a lead, but not a clear one yet.
    assert link_col["result"]["status"] == "unclear" and link_col["result"]["leader"] == "Calendly"
    assert link_col["result"]["emails_needed"] > 0 and not any(v["winner"] for v in link_col["values"])

    # Filters: any chosen value within a category, every category.
    one = client.get(f"/api/systems/{s['id']}", params={"value": f"{link['id']}:None"}, headers=AUTH).json()
    assert {e["idea"] for e in one["experiments"]} == {"Q1", "C1"} and one["emails"] == 80
    both = client.get(
        f"/api/systems/{s['id']}", params={"value": [f"{link['id']}:None", f"{hook['id']}:Question"]}, headers=AUTH
    ).json()
    assert [e["idea"] for e in both["experiments"]] == ["Q1"] and both["categories"][0]["result"]["status"] == "not_enough"
    unset = client.get(f"/api/systems/{s['id']}", params={"value": f"{hook['id']}:"}, headers=AUTH).json()
    assert [e["idea"] for e in unset["experiments"]] == ["No hook set"]
    assert client.get(f"/api/systems/{s['id']}", params={"value": "oops"}, headers=AUTH).status_code == 422


def test_category_rename_delete_and_value_checks(client):
    s = make_system(client)
    other = make_system(client)
    hook = add_category(client, s["id"], "Hook").json()
    foreign = add_category(client, other["id"], "Length").json()
    url = f"/api/systems/{s['id']}/experiments"
    assert client.post(url, json={"idea": "x", "values": {foreign["id"]: "Short"}}, headers=AUTH).status_code == 422
    assert client.post(url, json={"idea": "x", "values": {hook["id"]: "y" * 101}}, headers=AUTH).status_code == 422
    x = client.post(url, json={"idea": "x", "values": {hook["id"]: "Question"}}, headers=AUTH).json()

    assert client.patch(f"/api/categories/{hook['id']}", json={"name": "Opening line"}, headers=AUTH).json()["name"] == "Opening line"
    assert client.patch(f"/api/experiments/{x['id']}", json={"values": {hook["id"]: ""}}, headers=AUTH).json()["values"] == {}
    client.patch(f"/api/experiments/{x['id']}", json={"values": {hook["id"]: "Question"}}, headers=AUTH)
    assert client.patch(f"/api/experiments/{x['id']}", json={"idea": "y"}, headers=AUTH).json()["values"] == {hook["id"]: "Question"}

    assert client.delete(f"/api/categories/{hook['id']}", headers=AUTH).json() == {"ok": True}
    got = client.get(f"/api/systems/{s['id']}", headers=AUTH).json()
    assert got["categories"] == [] and got["experiments"][0]["values"] == {}
    assert client.delete(f"/api/categories/{hook['id']}", headers=AUTH).status_code == 404


def test_stats_show_what_moves_the_goal_and_what_went_together(client, clock):
    s = make_system(client, name="=HYPERLINK(1)")
    hook = add_category(client, s["id"], "Hook").json()["id"]
    link = add_category(client, s["id"], "Link").json()["id"]
    url = f"/api/systems/{s['id']}/experiments"
    # Question always went out with Calendly: their effects cannot be told apart.
    for camp, h, lnk in (("Q", "Question", "Calendly"), ("C", "Compliment", "None")):
        client.post(url, json={"idea": f"-{camp}", "campaign": camp, "values": {hook: h, link: lnk}}, headers=AUTH)
    send(client, "Q", 30, 12)
    send(client, "C", 30, 2)

    st = client.get(f"/api/systems/{s['id']}/stats", headers=AUTH).json()
    assert (st["goal"], st["emails"]) == ("reply_rate", 60)
    top = st["effects"][0]
    assert (top["value"], top["emails"], top["rate"], top["rate_without"], top["significant"]) == (
        "Question", 30, 0.4, 2 / 30, True)
    labels = [lb.get("value") or lb.get("name") for lb in st["matrix"]["labels"]]
    assert labels == ["Compliment", "Question", "Calendly", "None", "opened", "replied"]
    r = st["matrix"]["r"]
    assert r[1][2] == 1 and r[1][0] == -1 and r[1][5] > 0.3 and r[4][4] is None  # nothing was opened
    assert {(c["a"]["value"], c["b"]["value"]) for c in st["confounded"]} >= {("Question", "Calendly")}

    narrowed = client.get(f"/api/systems/{s['id']}/stats", params={"value": f"{hook}:Question"}, headers=AUTH).json()
    assert narrowed["emails"] == 30 and all(e["rate_without"] is None for e in narrowed["effects"])

    csv = client.get(f"/api/systems/{s['id']}/export.csv", headers=AUTH)
    assert csv.headers["content-type"].startswith("text/csv")
    assert 'filename="likelyread-hyperlink-1.csv"' in csv.headers["content-disposition"]
    lines = csv.text.strip().splitlines()
    assert lines[0] == "email_id,sent_at,campaign,test,Hook,Link,opened,opens,replied,replied_at,hours_to_reply"
    assert len(lines) == 61 and ",'-Q,Question,Calendly,0,0,1," in csv.text
    assert "bob@x.com" not in csv.text
