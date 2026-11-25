#!/usr/bin/env python3
"""Fetch an image from Docker Hub without docker and unpack its filesystem (stdlib only).

    fetch_docker_image.py REPO TAG OUTROOT      e.g. p4lang/behavioral-model latest ~/bmbench/docker

Writes OUTROOT/<tag>-<digest12>/:
  rootfs/            the merged layers (whiteouts applied; absolute symlinks re-rooted inside rootfs)
  manifest.json      the linux/amd64 image manifest
  config.json        the image config, including the build history (the RUN lines that built it)
  FETCH-INFO         repo, tag, index digest, manifest digest, layer digests, fetched_at

Running the extracted binary needs no container: invoke rootfs's own dynamic loader with
--library-path pointing into rootfs (bench.py does this for builds named docker:<dir>).
"""
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.request

ACCEPT_INDEX = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, headers, follow_without_auth=True):
    """GET with headers; a redirect (blob CDN) is followed WITHOUT the registry token."""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=headers)
    try:
        return opener.open(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and follow_without_auth:
            return urllib.request.urlopen(e.headers["Location"], timeout=600)
        raise


def main():
    repo, tag, outroot = sys.argv[1], sys.argv[2], os.path.expanduser(sys.argv[3])
    tok = json.load(urllib.request.urlopen(
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull", timeout=60))["token"]
    auth = {"Authorization": f"Bearer {tok}"}
    base = f"https://registry-1.docker.io/v2/{repo}"
    r = get(f"{base}/manifests/{tag}", {**auth, "Accept": ACCEPT_INDEX})
    index_digest = r.headers.get("Docker-Content-Digest")
    top = json.load(r)
    if "manifests" in top:
        cand = [m for m in top["manifests"] if m.get("platform", {}).get("architecture") == "amd64"
                and m.get("platform", {}).get("os") == "linux"]
        if not cand:
            sys.exit("no linux/amd64 manifest")
        mdig = cand[0]["digest"]
        manifest = json.load(get(f"{base}/manifests/{mdig}", {**auth, "Accept": cand[0]["mediaType"]}))
    else:
        mdig, manifest = index_digest, top
    out = os.path.join(outroot, f"{tag}-{mdig.split(':')[1][:12]}")
    if os.path.exists(os.path.join(out, "FETCH-INFO")):
        print(f"already fetched: {out}")
        return
    rootfs = os.path.join(out, "rootfs")
    os.makedirs(rootfs, exist_ok=True)
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=1)
    config = json.load(get(f"{base}/blobs/{manifest['config']['digest']}", auth))
    json.dump(config, open(os.path.join(out, "config.json"), "w"), indent=1)

    for i, layer in enumerate(manifest["layers"]):
        dig = layer["digest"]
        path = os.path.join(out, f"layer{i}.tar")
        h = hashlib.sha256()
        with get(f"{base}/blobs/{dig}", auth) as src, open(path, "wb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
                dst.write(chunk)
        if "sha256:" + h.hexdigest() != dig:
            sys.exit(f"layer {i} digest mismatch")
        with tarfile.open(path) as t:
            members = []
            for m in t.getmembers():
                name = os.path.normpath(m.name.lstrip("/"))
                if name.startswith(".."):
                    continue
                d, b = os.path.split(name)
                if b == ".wh..wh..opq":                       # opaque dir: drop lower-layer content
                    p = os.path.join(rootfs, d)
                    if os.path.isdir(p):
                        for e in os.listdir(p):
                            q = os.path.join(p, e)
                            shutil.rmtree(q) if os.path.isdir(q) and not os.path.islink(q) else os.remove(q)
                    continue
                if b.startswith(".wh."):                        # whiteout: delete from lower layers
                    p = os.path.join(rootfs, d, b[4:])
                    if os.path.lexists(p):
                        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
                    continue
                if m.isdev():
                    continue
                m.name = name
                members.append(m)
            t.extractall(rootfs, members=members, filter="tar")
        os.remove(path)
        print(f"layer {i}: {dig[:19]} ({layer.get('size', 0) / 1e6:.1f} MB)")

    # Re-root absolute symlinks so nothing inside rootfs resolves to the host's files.
    fixed = 0
    for dp, dns, fns in os.walk(rootfs):
        for n in dns + fns:
            p = os.path.join(dp, n)
            if os.path.islink(p):
                tgt = os.readlink(p)
                if tgt.startswith("/"):
                    os.remove(p)
                    os.symlink(os.path.relpath(os.path.join(rootfs, tgt.lstrip("/")), dp), p)
                    fixed += 1
    with open(os.path.join(out, "FETCH-INFO"), "w") as f:
        f.write(f"repo={repo}\ntag={tag}\nindex_digest={index_digest}\nmanifest_digest={mdig}\n")
        f.write(f"layers={','.join(l['digest'] for l in manifest['layers'])}\n")
        f.write(f"image_created={config.get('created')}\nabsolute_symlinks_rerooted={fixed}\n")
        f.write(f"fetched_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    print(f"done: {out}")


if __name__ == "__main__":
    main()
