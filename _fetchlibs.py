"""Fetch + extract the exact shared libs chrome-headless-shell needs,
no root required (dpkg -x into /tmp/alibs). Ubuntu noble amd64 debs
fetched over HTTPS from archive.ubuntu.com."""
import os, subprocess, urllib.request, ssl, re, sys, tarfile

DEBS = {
    # lib -> source package filename stem we search for in the Packages index
    "libglib2.0-0t64": "glib2.0",
    "libnss3": "nss",
    "libsqlite3-0": "sqlite3",
    "libnspr4": "nspr",
    "libdbus-1-3": "dbus",
    "libatk1.0-0t64": "atk1.0",
    "libatk-bridge2.0-0t64": "at-spi2-atk",
    "libcups2t64": "cups",
    "libdrm2": "libdrm",
    "libxkbcommon0": "libxkbcommon",
    "libatspi2.0-0t64": "at-spi2-core",
    "libx11-6": "libx11",
    "libxcomposite1": "libxcomposite",
    "libxdamage1": "libxdamage",
    "libxext6": "libxext",
    "libxfixes3": "libxfixes",
    "libxrandr2": "libxrandr",
    "libgbm1": "mesa",
    "libpango-1.0-0": "pango1.0",
    "libcairo2": "cairo",
    "libasound2t64": "alsa-lib",
    "libexpat1": "expat",
    "libxcb1": "libxcb",
    "libxshmfence1": "libxshmfence",
    "libxxf86vm1": "libxxf86vm",
    "libxtst6": "libxtst",
    "libxrender1": "libxrender",
    "libxi6": "libxi",
    "libudev1": "systemd",
    "libcap2": "libcap2",
    "libtinfo6": "ncurses",
}
BASE = "/tmp/debs"
OUT = "/tmp/alibs"
os.makedirs(BASE, exist_ok=True)
os.makedirs(OUT, exist_ok=True)


def extract_deb(deb_path: str, out_dir: str):
    """Extract a .deb without dpkg-deb: read the ar archive manually,
    find data.tar.{xz,zst,gz}, untar into out_dir."""
    import struct
    with open(deb_path, "rb") as fh:
        magic = fh.read(8)
        if magic != b"!<arch>\n":
            raise RuntimeError(f"not a deb: {deb_path}")
        while True:
            hdr = fh.read(60)
            if len(hdr) < 60:
                break
            name = hdr[:16].decode("ascii", "replace").strip()
            size = int(hdr[48:58].decode("ascii", "replace").strip() or 0)
            body = fh.read(size)
            if name.startswith("data.tar"):
                import io
                tar_data = body
                # strip ar member padding already handled by size
                if name.endswith(".xz"):
                    import lzma
                    tar_data = lzma.decompress(body)
                elif name.endswith(".gz"):
                    import gzip
                    tar_data = gzip.decompress(body)
                elif name.endswith(".zst"):
                    import zstandard
                    dctx = zstandard.ZstdDecompressor()
                    # ar pads members to even length — a stray leading byte
                    # breaks the zstd magic; find the real frame start (0x28
                    # b5 2f fd) and slice to it.
                    i = body.find(b"\x28\xb5\x2f\xfd")
                    if i > 0:
                        body = body[i:]
                    tar_data = dctx.stream_reader(io.BytesIO(body)).read()
                with tarfile.open(fileobj=io.BytesIO(tar_data)) as tf:
                    tf.extractall(out_dir, filter="data")
                return
    raise RuntimeError(f"no data.tar in {deb_path}")

# fetch Packages index for noble main (gzip)
idx_url = "https://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz"
print("fetching Packages index...", flush=True)
import gzip
raw = gzip.decompress(urllib.request.urlopen(idx_url, timeout=60).read()).decode(
    "utf-8", "replace")
blocks = raw.split("\n\n")
print(f"{len(blocks)} packages in index", flush=True)

wanted = set(DEBS.keys())
# also unity-gtk tear? not needed. Map: package name -> Filename
found = {}
for b in blocks:
    m = re.search(r"^Package: (\S+)$", b, re.M)
    if not m:
        continue
    name = m.group(1)
    if name in wanted and name not in found:
        f = re.search(r"^Filename: (\S+)$", b, re.M)
        if f:
            found[name] = f.group(1)
missing = wanted - set(found)
if missing:
    print("MISSING from index:", missing, flush=True)

for name, fn in found.items():
    dest = os.path.join(BASE, os.path.basename(fn))
    if not os.path.exists(dest):
        url = "https://archive.ubuntu.com/ubuntu/" + fn
        print(f"fetch {name}: {os.path.basename(fn)}", flush=True)
        urllib.request.urlretrieve(url, dest)
    # extract (no dpkg-deb on this box — a .deb is an ar archive:
    # data.tar.xz holds the filesystem; extract with ar/tar via python)
    extract_deb(dest, OUT)

# also fetch liberation + dejavu fonts (fontconfig deps for Skia)
fonts = {
    "fonts-liberation": "fonts-liberation",
    "fonts-dejavu-core": "fonts-dejavu",
}
for name, src in fonts.items():
    for b in blocks:
        m = re.search(r"^Package: (\S+)$", b, re.M)
        if m and m.group(1) == name:
            f = re.search(r"^Filename: (\S+)$", b, re.M)
            if f:
                dest = os.path.join(BASE, os.path.basename(f.group(1)))
                if not os.path.exists(dest):
                    urllib.request.urlretrieve(
                        "https://archive.ubuntu.com/ubuntu/" + f.group(1), dest)
                extract_deb(dest, OUT)
            break

print("DONE. libs in /tmp/alibs:", flush=True)
n = sum(len(f) for _, _, f in os.walk(OUT))
print(f"total files: {n}", flush=True)
