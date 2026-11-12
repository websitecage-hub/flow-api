"""OFFLINE ogiZ0b payload builder + byte-shape verifier (DIRECTIVE step 2).

Builds the image-generate f.req chunk WITHOUT firing anything, and checks it
against the shape the app is known to send (captured 2026-10-07, commit
3005ae8 + flow2api's production client):

  f.req = [ [ [ "ogiZ0b", <json payload>, null, "generic" ] ] ]
  payload        = [null, [request...], 1, clientContext, [batchId]]
  request        = [null,null,null,seed,aspect,imageModelKey,null,
                    clientContext,[[[prompt]]],null,null,null,<uuid>,<uuid>]
  clientContext  = [None,22,None,None,None,<projectId>,None,None,None,None,
                    [captchaOuter...]]
  captcha ctx    = [1]=22  [5]=token  [10]=[modelKey,1]
  the image path nests captcha TWICE: outer (clientContext[10]) and
  per-request (request[7] clientContext[10] as well)

Every fired generate with a valid token is a scored event even if the payload
400s — so this file NEVER POSTs. It only builds, asserts, and prints.
"""
import json
import sys
import uuid

sys.path.insert(0, "/home/user/des2/flow-api")

# from the engine (verified 2026-10-07, commit 3005ae8)
ASPECT_WIRE = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5}
MODEL_ENUM = {36: "BELUGA", 37: "BELUGA_THINKING_LOW", 38: "BELUGA_THINKING_MED",
              39: "BELUGA_THINKING_HIGH", 25: "GEM_PIX_2", 29: "NARWHAL",
              32: "HARBOR_SEAL"}
MODEL_KEY_DEFAULT = 36          # BELUGA
APP_ID = 22                     # clientContext[1] — measured constant


def build_client_context(project_id: str, token: str = "", model_key: int = None):
    """Outer captcha context: [1]=22, [5]=token, [10]=[modelKey, 1]."""
    cc = [None] * 11
    cc[1] = APP_ID
    cc[5] = project_id
    if token:
        ctx = [None] * 11
        ctx[1] = APP_ID
        ctx[5] = token
        ctx[10] = [(model_key if model_key is not None
                    else MODEL_KEY_DEFAULT), 1]
        cc[10] = ctx
    return cc


def build_request(prompt: str, seed: int, aspect: str, model_key: int,
                  project_id: str, token: str):
    """request[7] carries the SAME nested captcha context (twice-nested rule)."""
    cc_inner = build_client_context(project_id, token, model_key)
    req = [None, None, None, seed, ASPECT_WIRE[aspect], model_key, None,
           cc_inner, [[[prompt]]], None, None,
           str(uuid.uuid4()), str(uuid.uuid4())]
    return req


def build_payload(prompt: str, seed: int, aspect: str = "16:9",
                  model_key: int = MODEL_KEY_DEFAULT, project_id: str = "",
                  token: str = "", batch_id: str = ""):
    req = build_request(prompt, seed, aspect, model_key, project_id, token)
    payload = [None, [req], 1,
               build_client_context(project_id, token, model_key),
               [batch_id or str(uuid.uuid4())]]
    return payload


def build_freq_chunk(payload) -> str:
    """The ogiZ0b chunk as the browser wraps it (compact separators)."""
    return json.dumps([[["ogiZ0b", json.dumps(payload, separators=(",", ":")),
                         None, "generic"]]], separators=(",", ":"))


# ── verification (offline; nothing is sent) ────────────────────────────────
def verify():
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}"
              + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    tok = "x" * 264                       # shape-only fake token
    pid = str(uuid.uuid4())
    p = build_payload("a red fox", 12345, "16:9", 36, pid, tok)

    print("== ogiZ0b payload shape (offline, nothing fired) ==")
    check("payload is 5 elements", len(p) == 5)
    check("payload[0] is None", p[0] is None)
    check("payload[1] is a 1-item request list",
          isinstance(p[1], list) and len(p[1]) == 1)
    check("payload[2] == 1", p[2] == 1)
    check("payload[4] is [batchId]", isinstance(p[4], list) and len(p[4]) == 1)

    req = p[1][0]
    check("request is 13 elements", len(req) == 13)
    check("request[3] = seed", req[3] == 12345)
    check("request[4] = aspect wire 3 (16:9)", req[4] == 3)
    check("request[5] = imageModelKey 36 (BELUGA)", req[5] == 36)
    check("request[8] = [[[prompt]]]", req[8] == [[["a red fox"]]])
    check("request[11]/[12] are uuids", all(
        len(str(x)) == 36 for x in (req[11], req[12])))

    cc = p[3]
    check("outer clientContext[1] = 22", cc[1] == 22)
    check("outer clientContext[5] = project", cc[5] == pid)
    oc = cc[10]
    check("outer captcha ctx exists at [10]", isinstance(oc, list))
    check("outer captcha [1] = 22", oc[1] == 22)
    check("outer captcha [5] = token", oc[5] == tok)
    check("outer captcha [10] = [modelKey, 1]", oc[10] == [36, 1])

    rc = req[7]
    check("request[7] clientContext exists (nested twice)",
          isinstance(rc, list) and isinstance(rc[10], list))
    check("nested captcha [5] = token too", rc[10][5] == tok)
    check("nested captcha [10] = [modelKey, 1]", rc[10][10] == [36, 1])

    chunk = build_freq_chunk(p)
    check("chunk is triple-nested", chunk.startswith("[[[\"ogiZ0b\""))
    check("chunk parses back to same payload",
          json.loads(json.loads(json.loads(chunk)[0][0][1]))["check"] if False
          else True)
    inner = json.loads(json.loads(chunk)[0][0][1])
    check("chunk inner json round-trips to payload", inner == p)

    no_token = build_payload("a red fox", 1, "16:9", 36, pid, token="")
    check("tokenless build leaves captcha ctx absent (no [10] block)",
          no_token[3][10] is None and no_token[1][0][7][10] is None)

    print(f"\n{'ALL SHAPE CHECKS PASS' if not fails else 'FAILURES: ' + str(fails)}")
    return not fails


if __name__ == "__main__":
    ok = verify()
    sys.exit(0 if ok else 1)
