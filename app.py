"""
JLG Showing Packet Builder -- web edition (Render-ready)
----------------------------------------------------------
Same packet-building pipeline as the desktop app, adapted to run on a host
with an ephemeral filesystem (Render's free tier wipes local disk on every
restart):

- Agent phone/email default from environment variables, not a config file.
- Every request is fully self-contained: the browser holds the uploaded
  listing sheets in memory across the "sequence" step and re-sends them on
  /generate, so the server never needs to remember anything about a batch
  between requests. The merged packet PDF is built to a temp file for the
  duration of one request and streamed straight back -- nothing about a
  showing or a listing sits on the server afterward.
"""
import json
import os
import tempfile
import traceback
from datetime import datetime

from flask import Flask, request, jsonify, send_file, render_template_string, after_this_request

from mls_router import parse_listing_pdf
from packet import build_packet, split_into_listing_pdfs

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300MB total upload cap

DEFAULT_AGENT_NAME = os.environ.get("AGENT_NAME", "Brian Elmore")
DEFAULT_AGENT_PHONE = os.environ.get("AGENT_PHONE", "")
DEFAULT_AGENT_EMAIL = os.environ.get("AGENT_EMAIL", "brian@justinlucasgroup.com")


PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>JLG Showing Packet Builder</title>
<style>
  body { font-family: -apple-system, 'Work Sans', sans-serif; background:#f2f2f2; margin:0; padding:0; color:#222; }
  .wrap { max-width: 820px; margin: 0 auto; padding: 36px 24px 80px; }
  header { display:flex; align-items:center; gap:14px; margin-bottom: 10px; }
  header h1 { font-size: 19px; margin:0; color:#032b42; }
  .sub { font-size: 13px; color:#666; margin-bottom: 24px; }
  .card { background:#fff; border-radius:8px; padding:24px; margin-bottom:20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .card h2 { font-size: 13.5px; color:#032b42; margin-bottom: 14px; text-transform: uppercase; letter-spacing: 0.04em; }
  #dropzone {
    border: 2px dashed #032b42; border-radius:8px; padding: 40px 20px; text-align:center;
    color:#032b42; cursor:pointer; transition: background 0.15s;
  }
  #dropzone.drag { background:#eef3f6; }
  #dropzone p { margin: 6px 0; }
  #dropzone .hint { font-size:12.5px; color:#888; }
  input[type=file] { display:none; }
  .settings-row { display:flex; gap:14px; flex-wrap:wrap; }
  .settings-row > div { flex: 1 1 200px; }
  .settings-row label { font-size:12.5px; color:#444; display:block; margin-bottom:4px; }
  .settings-row input[type=text] {
    padding:8px 10px; border:1px solid #ccc; border-radius:5px; font-size:13.5px; width:100%;
  }
  button.primary {
    background:#032b42; color:#fff; border:none; padding:10px 18px; border-radius:5px;
    font-size:13.5px; cursor:pointer; margin-top:14px;
  }
  button.primary:hover { background:#04405f; }
  button.primary:disabled { background:#9aa7ae; cursor:default; }
  button.secondary {
    background:#fff; color:#032b42; border:1px solid #032b42; padding:9px 16px; border-radius:5px;
    font-size:13px; cursor:pointer;
  }
  #status { font-size:13px; color:#666; margin-top:10px; }
  #errors { margin-top: 10px; }
  .error-row { color:#780000; font-size:12.5px; padding:4px 0; }

  #stepSequence { display:none; }

  .seq-row {
    display:flex; align-items:center; gap:12px; background:#fafbfb; border:1px solid #e6e8ea;
    border-radius:6px; padding:10px 12px; margin-bottom:8px; cursor:grab;
  }
  .seq-row.dragging { opacity: 0.4; }
  .seq-row .handle { color:#b7bcbe; font-size:14px; user-select:none; }
  .seq-row .badge {
    flex: 0 0 24px; height:24px; border-radius:50%; background:#032b42; color:#fff;
    font-size:12px; font-weight:700; display:flex; align-items:center; justify-content:center;
  }
  .seq-row .info { flex:1; min-width:0; }
  .seq-row .info .addr { font-weight:600; font-size:13px; color:#222; }
  .seq-row .info .facts { font-size:11.5px; color:#888; margin-top:1px; }
  .seq-row input[type=text].time-input {
    width: 110px; padding:7px 9px; border:1px solid #ccc; border-radius:5px; font-size:13px;
  }
  .seq-row .remove { color:#9a9a9c; cursor:pointer; font-size:12px; padding:0 4px; }
  .seq-row .remove:hover { color:#780000; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Justin Lucas Group &mdash; Showing Packet Builder</h1>
  </header>
  <div class="sub">Drop in raw MLS listing sheets for a showing, put them in order, add times, and get back one branded PDF packet: cover page + route map + each listing's flyer, in order. Nothing is stored on the server.</div>

  <div class="card" id="stepParse">
    <h2>1. Add listings</h2>
    <div id="dropzone">
      <p><strong>Drag &amp; drop listing sheet PDF(s) here</strong></p>
      <p class="hint">or click to browse &mdash; select every property on today's showing list at once</p>
      <input type="file" id="fileInput" accept="application/pdf" multiple>
    </div>
    <div style="font-size:11.5px;color:#888;margin-top:8px;">
      MichRIC (Michigan) listings: export the <strong>NEW MichRIC Full Detail Report</strong> format &mdash; the one with a "Property Features" grid (Exterior / Interior / Construction-Utilities columns) and a "Tax and Legal" section. The older single-column report layout isn't supported and will come back mostly blank.
    </div>
    <div id="status"></div>
    <div id="errors"></div>
  </div>

  <div class="card" id="stepSequence">
    <h2>2. Drag into showing order &amp; enter a time for each stop</h2>
    <div style="font-size:12px;color:#888;margin:-8px 0 14px;">Drag the &#9776; handle to reorder. Times are optional &mdash; leave any stop blank and it'll show as "Time TBD" on the packet.</div>
    <div id="seqList"></div>
    <button class="secondary" id="addMoreBtn" type="button">+ Add more listings</button>
  </div>

  <div class="card" id="stepDetails" style="display:none;">
    <h2>3. Packet details</h2>
    <div class="settings-row">
      <div>
        <label>Showing date</label>
        <input type="text" id="showingDate" placeholder="e.g. Tuesday, July 8">
      </div>
      <div>
        <label>Client / buyer name (optional, shown as packet title)</label>
        <input type="text" id="clientName" placeholder="e.g. The Martinez Family">
      </div>
    </div>
    <div class="settings-row" style="margin-top:12px;">
      <div>
        <label>Prepared by / agent name</label>
        <input type="text" id="agentName">
      </div>
      <div>
        <label>Phone</label>
        <input type="text" id="agentPhone">
      </div>
      <div>
        <label>Email</label>
        <input type="text" id="agentEmail">
      </div>
    </div>
    <div style="font-size:11.5px;color:#888;margin-top:8px;">
      Building for someone else on the team? Change the name above first &mdash; e.g. Justin, Eric, or Camille. Remembered on this browser only.
    </div>
    <label style="display:flex;align-items:center;gap:7px;margin-top:14px;font-size:12.5px;color:#444;cursor:pointer;">
      <input type="checkbox" id="printSafeLogo" style="margin:0;">
      Print-safe logo (black &amp; white)
    </label>
    <label style="display:flex;align-items:center;gap:7px;margin-top:8px;font-size:12.5px;color:#444;cursor:pointer;">
      <input type="checkbox" id="includeMap" checked style="margin:0;">
      Include route map on cover page (free OpenStreetMap geocoding &mdash; adds ~1 sec/stop)
    </label>

    <button class="primary" id="generateBtn" type="button">Generate Showing Packet</button>
    <div id="genStatus" style="font-size:13px;color:#666;margin-top:10px;"></div>
  </div>
</div>

<script>
const dz = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const statusEl = document.getElementById('status');
const errorsEl = document.getElementById('errors');
const stepSequence = document.getElementById('stepSequence');
const stepDetails = document.getElementById('stepDetails');
const seqList = document.getElementById('seqList');
const addMoreBtn = document.getElementById('addMoreBtn');
const generateBtn = document.getElementById('generateBtn');
const genStatus = document.getElementById('genStatus');
const nameEl = document.getElementById('agentName');
const phoneEl = document.getElementById('agentPhone');
const emailEl = document.getElementById('agentEmail');
const printSafeLogoEl = document.getElementById('printSafeLogo');

nameEl.value = localStorage.getItem('jlg_agent_name') || '{{ default_name }}';
phoneEl.value = localStorage.getItem('jlg_agent_phone') || '{{ default_phone }}';
emailEl.value = localStorage.getItem('jlg_agent_email') || '{{ default_email }}';
printSafeLogoEl.checked = localStorage.getItem('jlg_print_safe_logo') === '1';
nameEl.addEventListener('change', () => localStorage.setItem('jlg_agent_name', nameEl.value));
phoneEl.addEventListener('change', () => localStorage.setItem('jlg_agent_phone', phoneEl.value));
emailEl.addEventListener('change', () => localStorage.setItem('jlg_agent_email', emailEl.value));
printSafeLogoEl.addEventListener('change', () => localStorage.setItem('jlg_print_safe_logo', printSafeLogoEl.checked ? '1' : '0'));

// `uploadedFiles` holds every unique File the user has dropped in, in
// upload order -- this is what actually gets sent to the server (once
// each, even if a file expands into several listings below). Each sequence
// item just points back at a (fileIndex, subIndex) pair rather than
// holding its own File reference, since one uploaded file (a batch export)
// can expand into several independently-orderable rows.
let uploadedFiles = [];
let items = []; // { id, fileIndex, subIndex, address_line1, city_state_zip, price, beds, baths, sqft, time }
let dragSrcId = null;

dz.addEventListener('click', () => fileInput.click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault();
  dz.classList.remove('drag');
  handleFiles(e.dataTransfer.files);
});
fileInput.addEventListener('change', () => { handleFiles(fileInput.files); fileInput.value = ''; });
addMoreBtn.addEventListener('click', () => fileInput.click());

function handleFiles(fileList) {
  if (!fileList || !fileList.length) return;
  const newFiles = Array.from(fileList);
  const baseIndex = uploadedFiles.length;
  uploadedFiles = uploadedFiles.concat(newFiles);

  const form = new FormData();
  for (const f of newFiles) form.append('files', f);

  errorsEl.innerHTML = '';
  statusEl.textContent = 'Reading ' + newFiles.length + ' file(s)...';

  fetch('/preview', { method: 'POST', body: form })
    .then(r => r.json())
    .then(data => {
      let addedOk = 0;
      data.results.forEach(r => {
        if (r.ok) {
          items.push({
            id: 'l' + Math.random().toString(36).slice(2, 10),
            fileIndex: baseIndex + r.file_index,
            subIndex: r.sub_index,
            address_line1: r.address_line1,
            city_state_zip: r.city_state_zip,
            price: r.price, beds: r.beds, baths: r.baths, sqft: r.sqft,
            time: '',
          });
          addedOk++;
        } else {
          const row = document.createElement('div');
          row.className = 'error-row';
          row.textContent = r.source + ': could not parse (' + r.error + ')';
          errorsEl.appendChild(row);
        }
      });
      const total = data.results.length;
      statusEl.textContent = addedOk + ' listing(s) added' + (total !== addedOk ? ' (' + (total - addedOk) + ' could not be read — see below)' : '') + '.';
      renderSeqList();
      if (items.length) {
        stepSequence.style.display = 'block';
        stepDetails.style.display = 'block';
      }
    })
    .catch(err => { statusEl.textContent = 'Error: ' + err; });
}

function renderSeqList() {
  seqList.innerHTML = '';
  items.forEach((item, idx) => {
    const row = document.createElement('div');
    row.className = 'seq-row';
    row.draggable = true;
    row.dataset.id = item.id;

    row.addEventListener('dragstart', () => { dragSrcId = item.id; row.classList.add('dragging'); });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));
    row.addEventListener('dragover', e => e.preventDefault());
    row.addEventListener('drop', e => {
      e.preventDefault();
      if (!dragSrcId || dragSrcId === item.id) return;
      const fromIdx = items.findIndex(x => x.id === dragSrcId);
      const toIdx = items.findIndex(x => x.id === item.id);
      const [moved] = items.splice(fromIdx, 1);
      items.splice(toIdx, 0, moved);
      renderSeqList();
    });

    const facts = [item.price, item.beds, item.baths, item.sqft].filter(Boolean).join(' · ');

    row.innerHTML = `
      <span class="handle">☰</span>
      <span class="badge">${idx + 1}</span>
      <div class="info">
        <div class="addr">${item.address_line1}</div>
        <div class="facts">${item.city_state_zip}${facts ? ' · ' + facts : ''}</div>
      </div>
      <input type="text" class="time-input" placeholder="10:00 AM (optional)" value="${item.time}">
      <span class="remove" title="Remove">✕</span>
    `;
    const timeInput = row.querySelector('.time-input');
    timeInput.addEventListener('input', e => {
      item.time = e.target.value;
    });
    row.querySelector('.remove').addEventListener('click', () => {
      items = items.filter(x => x.id !== item.id);
      renderSeqList();
      if (!items.length) { stepSequence.style.display = 'none'; stepDetails.style.display = 'none'; }
    });
    seqList.appendChild(row);
  });
}

generateBtn.addEventListener('click', () => {
  if (!items.length) return;

  // Times are optional -- a stop left blank just renders as "Time TBD"
  // on the cover page (see cover.html's .sched-time.tbd), so an ordered
  // list without confirmed times yet is a legitimate, generatable packet
  // on its own, not an error state to block on.
  genStatus.style.color = '#666';

  generateBtn.disabled = true;
  genStatus.textContent = 'Building packet' + (document.getElementById('includeMap').checked ? ' (geocoding stops for the map, this takes a few seconds)...' : '...');

  const form = new FormData();
  uploadedFiles.forEach(f => form.append('files', f));
  const order = items.map(i => ({ file_index: i.fileIndex, sub_index: i.subIndex, time: i.time }));
  form.append('order', JSON.stringify(order));
  form.append('showing_date', document.getElementById('showingDate').value);
  form.append('client_name', document.getElementById('clientName').value);
  form.append('agent_name', nameEl.value);
  form.append('agent_phone', phoneEl.value);
  form.append('agent_email', emailEl.value);
  form.append('print_safe_logo', printSafeLogoEl.checked ? '1' : '');
  form.append('include_map', document.getElementById('includeMap').checked ? '1' : '');

  fetch('/generate', { method: 'POST', body: form })
    .then(async r => {
      if (!r.ok) { throw new Error(await r.text()); }
      return r.blob();
    })
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'JLG_Showing_Packet.pdf';
      a.click();
      genStatus.textContent = 'Done — packet downloaded.';
      generateBtn.disabled = false;
    })
    .catch(err => {
      genStatus.textContent = 'Error building packet: ' + err;
      generateBtn.disabled = false;
    });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        PAGE,
        default_name=DEFAULT_AGENT_NAME,
        default_phone=DEFAULT_AGENT_PHONE,
        default_email=DEFAULT_AGENT_EMAIL,
    )


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/preview", methods=["POST"])
def preview():
    # A single uploaded PDF is usually one listing, but agents commonly
    # batch-export several listings into one combined MRED file -- split
    # each upload into its component listing(s) first so every address
    # inside it shows up as its own row, not just the first one. Each
    # result is tagged with (file_index, sub_index) so the frontend can
    # track it back to the specific uploaded file + page range it came
    # from once the sheets get reordered.
    files = request.files.getlist("files")
    results = []
    for file_index, f in enumerate(files):
        source_name = f.filename or "listing.pdf"
        try:
            data = f.read()
            blobs = split_into_listing_pdfs(data)
        except Exception as e:
            traceback.print_exc()
            results.append({"ok": False, "file_index": file_index, "sub_index": 0, "source": source_name, "error": str(e)})
            continue

        multi = len(blobs) > 1
        for sub_index, blob in enumerate(blobs):
            label = source_name if not multi else f"{source_name} (listing {sub_index + 1} of {len(blobs)})"
            try:
                listing = parse_listing_pdf(blob, label)
                if not listing.address_line1:
                    raise ValueError("no address found on this sheet")
                results.append({
                    "ok": True,
                    "file_index": file_index,
                    "sub_index": sub_index,
                    "source": label,
                    "address_line1": listing.address_line1,
                    "city_state_zip": listing.city_state_zip,
                    "price": listing.list_price,
                    "beds": f"{listing.bedrooms} bd" if listing.bedrooms else "",
                    "baths": f"{listing.bathrooms_display} ba" if listing.bathrooms_full else "",
                    "sqft": f"{listing.approx_sf} sf" if listing.approx_sf else "",
                })
            except Exception as e:
                traceback.print_exc()
                results.append({"ok": False, "file_index": file_index, "sub_index": sub_index, "source": label, "error": str(e)})
    return jsonify({"results": results})


@app.route("/generate", methods=["POST"])
def generate():
    agent_name = request.form.get("agent_name", "").strip() or DEFAULT_AGENT_NAME
    agent_phone = request.form.get("agent_phone", "").strip() or DEFAULT_AGENT_PHONE
    agent_email = request.form.get("agent_email", "").strip() or DEFAULT_AGENT_EMAIL
    print_safe_logo = bool(request.form.get("print_safe_logo", "").strip())
    include_map = request.form.get("include_map", "").strip() == "1"
    showing_date = request.form.get("showing_date", "").strip()
    client_name = request.form.get("client_name", "").strip()

    # `files` here is the deduplicated set of underlying uploads (each sent
    # once, even if it expanded into several listings in the sequence
    # step); `order` is the final showing sequence, each entry pointing
    # back at which uploaded file + which listing within it, plus that
    # stop's time. This split is what lets listings from the same combined
    # PDF be dragged apart and interleaved with listings from other files.
    files = request.files.getlist("files")
    try:
        order = json.loads(request.form.get("order", "[]"))
    except Exception:
        order = []

    file_blob_cache = {}

    def get_blob(file_index, sub_index):
        if file_index not in file_blob_cache:
            if file_index is None or file_index >= len(files):
                return None
            file_blob_cache[file_index] = split_into_listing_pdfs(files[file_index].read())
        blobs = file_blob_cache[file_index]
        if sub_index is None or sub_index >= len(blobs):
            return None
        return blobs[sub_index]

    ordered_items = []
    for entry in order:
        blob = get_blob(entry.get("file_index"), entry.get("sub_index", 0))
        if blob is None:
            continue
        try:
            source_name = files[entry["file_index"]].filename or "listing.pdf"
            listing = parse_listing_pdf(blob, source_name)
            ordered_items.append({"listing": listing, "time": (entry.get("time") or "").strip()})
        except Exception:
            traceback.print_exc()
            continue

    if not ordered_items:
        return "No valid listing sheets to build a packet from.", 400

    tmp_out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_out.close()

    try:
        build_packet(
            ordered_items,
            tmp_out.name,
            showing_date=showing_date,
            client_name=client_name,
            agent_name=agent_name,
            agent_phone=agent_phone,
            agent_email=agent_email,
            print_safe_logo=print_safe_logo,
            include_map=include_map,
            geocode_user_agent=f"jlg-showing-packet-app ({agent_email})",
            prepared_date=datetime.now().strftime("%B %-d, %Y"),
        )
    except Exception as e:
        traceback.print_exc()
        if os.path.exists(tmp_out.name):
            os.unlink(tmp_out.name)
        return f"Failed to build packet: {e}", 500

    @after_this_request
    def cleanup(response):
        try:
            os.unlink(tmp_out.name)
        except OSError:
            pass
        return response

    out_name = f"JLG_Showing_Packet_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(tmp_out.name, as_attachment=True, download_name=out_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
