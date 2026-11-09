import proxy
from gateway import frontend_shapes
from gateway.frontend_shapes import video_create, shapes
from tests.support.video_rig import run, MODEL


def test_probe(tmp_path):
    async def exercise(w):
        snap = proxy.WORKFLOW_SNAPSHOTS.current()
        rows = {r.id: r for r in frontend_shapes.bundled_rows()}
        row = rows["videos-create"]
        shape = frontend_shapes.build_shape(row)
        hooks = shapes.VIDEO_HOOKS or shapes.VideoHooks()
        resolved = hooks.resolve_model(MODEL)
        print("PROBE hooks", type(hooks), "resolved", resolved)
        picked = video_create.select(shape, {"prompt": "x", "model": MODEL}, row, snap, hooks)
        print("PROBE picked", type(picked), getattr(picked, "name", None))
        from gateway import video_grid
        names = [k for k in snap.entries] if hasattr(snap, "entries") else dir(snap)
        print("PROBE", names[:5])
        e = snap.get("video.generate.ltx-2.5-t2v")
        print("PROBE entry", e is not None, video_grid.video_facts(e) if e else None)
        r = await w.call("POST", "/v1/videos", json_body={"prompt": "x", "model": MODEL})
        print("PROBE resp", r.status_code, r.text[:300])
    run(tmp_path, exercise)
