"""Fixed component set. The model names a type + result_id; rows come from the server."""

from __future__ import annotations

from ...money import rupees_to_paise

ALLOWED = {
    "find_payer": {"chips", "payer_card"},  # a card asked for on a search result is shown as chips
    "get_payer_summary": {"payer_card", "stat_cards"},
    "list_transactions": {"table", "stat_cards"},
    "get_sales_summary": {"line_chart", "stat_cards"},
    "list_dues": {"table", "stat_cards", "invoice_card"},
    "create_invoice": {"confirm_action", "invoice_card"},
    "send_reminder": {"confirm_action", "invoice_card"},
    "call_customer": {"confirm_action", "invoice_card"},
    "set_recurring": {"confirm_action"},
}


def _p(rupees) -> int | None:
    return None if rupees is None else rupees_to_paise(rupees)


def _stat_cards(tool: str, d: dict) -> dict:
    if tool == "get_payer_summary":
        items = [
            {"label": "Total paid", "value_paise": _p(d["total_rupees"])},
            {"label": "Payments", "value": d["payments"]},
            {"label": "Usual amount", "value_paise": _p(d["usual_amount_rupees"])},
            {"label": "Usual time", "value_text": d["usual_time"] or "—"},
        ]
    elif tool == "get_sales_summary":
        items = [
            {"label": "Collected", "value_paise": _p(d["total_rupees"])},
            {"label": "Payments", "value": d["payments"]},
            {"label": "Average per day", "value_paise": _p(d["average_per_day_rupees"])},
            {"label": "Best day", "value_paise": _p(d["best_day"]["total_rupees"]) if d["best_day"] else None,
             "sub": d["best_day"]["date"] if d["best_day"] else None},
        ]
    elif tool == "list_transactions":
        items = [{"label": "Total", "value_paise": _p(d["total_rupees"])}, {"label": "Payments", "value": d["payments"]}]
    else:  # list_dues
        items = [{"label": "Total", "value_paise": _p(d["total_rupees"])}, {"label": "Invoices", "value": d["count"]}]
    return {"items": items}


def _table(tool: str, d: dict) -> dict:
    if tool == "list_transactions":
        rows = d["_full_rows"]
        return {
            "kind": "payments",
            "title": f"{d['payments']} payment{'s' if d['payments'] != 1 else ''}",
            "total_rows": d["payments"],
            "total_paise": _p(d["total_rupees"]),
            "rows": [{"id": str(r["id"]), "paid_at": r["paid_at"].isoformat(), "customer": r["payer_name"],
                      "payer_id": str(r["payer_id"]) if r["payer_id"] else None, "amount_paise": r["amount_paise"],
                      "description": r["description"]} for r in rows],
        }
    return {
        "kind": "invoices",
        "title": f"{d['count']} invoice{'s' if d['count'] != 1 else ''}",
        "total_rows": d["count"],
        "total_paise": _p(d["total_rupees"]),
        "rows": [{"id": i["invoice_id"], "number": i["number"], "customer": i["customer"],
                  "amount_paise": _p(i["amount_rupees"]), "due_date": i["due_date"], "status": i["status"],
                  "days_overdue": i["days_overdue"]} for i in d["invoices"]],
    }


def _build(ctype: str, tool: str, d: dict, pending_by_token: dict) -> dict | None:
    if ctype == "chips":
        return {"choices": [{"payer_id": m["payer_id"], "label": m["name"], "sub": m["vpa"] or m["habit"]}
                            for m in d["matches"]]} if d["matches"] else None
    if ctype == "payer_card":
        return {"payer_id": d["payer_id"], "name": d["name"], "vpa": d["vpa"], "habit": d["lifetime"]["habit"],
                "badges": d["lifetime"]["badges"], "window": d["window"], "payments": d["payments"],
                "total_paise": _p(d["total_rupees"]), "usual_amount_paise": _p(d["usual_amount_rupees"]),
                "usual_time": d["usual_time"], "last_payment": d["last_payment"],
                "last_payment_was": d["last_payment_was"]}
    if ctype == "stat_cards":
        return _stat_cards(tool, d)
    if ctype == "line_chart":
        return {"grain": d["grain"], "points": [
            {"bucket": p["bucket"].isoformat(), "total_paise": p["total_paise"], "txn_count": p["txn_count"],
             **({"label_bucket": p["label_bucket"].isoformat()} if p.get("label_bucket") else {})}
            for p in d["_points"]]}
    if ctype == "table":
        return _table(tool, d)
    if ctype == "confirm_action":
        return pending_by_token.get(d.get("_token"))
    if ctype == "invoice_card":
        if tool == "list_dues":
            return {"invoice": d["invoices"][0]} if d["count"] == 1 else None
        action = pending_by_token.get(d.get("_token"))
        return {"preview": True, **action["summary"]} if action else None
    return None


def build_components(specs: list[dict], ctx) -> list[dict]:
    pending_by_token = {p["token"]: p for p in ctx.pending}
    out, seen = [], set()
    for spec in specs:
        ctype, rid = spec.get("type"), spec.get("result_id")
        result = ctx.results.get(rid)
        if result is None or ctype not in ALLOWED.get(result["tool"], set()) or (ctype, rid) in seen:
            continue
        if ctype == "payer_card" and result["tool"] == "find_payer":
            # A card asked for on the search result: use that customer's summary when it was looked up,
            # otherwise let the merchant pick the customer.
            found = result["data"]["matches"][0]["payer_id"] if result["data"].get("single_match") else None
            summary = next((k for k, r in ctx.results.items()
                            if r["tool"] == "get_payer_summary" and found and r["data"].get("payer_id") == found), None)
            if summary:
                rid, result = summary, ctx.results[summary]
            else:
                ctype = "chips"
            if (ctype, rid) in seen:
                continue
        seen.add((ctype, rid))
        data = _build(ctype, result["tool"], result["data"], pending_by_token)
        if data is not None:
            out.append({"type": ctype, "result_id": rid, "data": data})
    return out
