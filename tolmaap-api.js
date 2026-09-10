/**
 * Tolmaap - API Integration Layer
 * =================================
 * Wires the frontend camera/upload flow to the FastAPI + Tesseract OCR backend.
 *
 * HOW TO USE:
 *   Add ONE line to your HTML, just before </body>:
 *   <script src="tolmaap-api.js"></script>
 *
 * This script overrides beginAnalysis() with a real API call.
 * All existing functions (history, demo flows, WhatsApp share etc.) keep working.
 */

// ============================================================
const API_BASE = (typeof window !== 'undefined' && window.location.protocol.startsWith('http')) ? window.location.origin : 'http://localhost:8000';

// ============================================================
// OVERRIDE: beginAnalysis()
// Replaces the hardcoded 2.6-second fake timer with a real call.
// ============================================================
window.beginAnalysis = async function () {
  const capturedImg = document.getElementById('capturedImg');
  if (!capturedImg.src || capturedImg.src === window.location.href) {
    alert('No image captured -- please take a photo or upload one first.');
    return;
  }

  // Show the analyzing screen
  document.getElementById('analyzingThumb').src = capturedImg.src;
  if (typeof setStep === 'function') setStep('stepAnalyze');
  showScreen('screen-analyzing');

  // Cycle status messages while waiting for the OCR
  const statusMessages = [
    'Uploading image...',
    'Running OCR -- reading label text...',
    'Extracting MRP and net quantity...',
    'Checking manufacturing date...',
    'Validating against PCR 2011 rules...',
    'Preparing compliance report...',
  ];
  let msgIdx = 0;
  const statusEl = document.getElementById('analyzingStatus');
  statusEl.textContent = statusMessages[0];
  const ticker = setInterval(() => {
    msgIdx = (msgIdx + 1) % statusMessages.length;
    statusEl.textContent = statusMessages[msgIdx];
  }, 850);

  try {
    const blob    = await _dataUrlToBlob(capturedImg.src);
    const apiData = await _callAnalyzeAPI(blob);
    clearInterval(ticker);
    if (typeof setStep === 'function') setStep('stepResult');
    renderResultsFromAPI(apiData);
    if (typeof showTab === 'function') {
      showTab('new');
    } else {
      showScreen('screen-scan');
    }
  } catch (err) {
    clearInterval(ticker);
    _handleAnalysisError(err);
  }
};

// ============================================================
// MAIN: renderResultsFromAPI(apiData)
// Maps backend JSON to the existing result screen.
// Populates lastResult so WhatsApp / read-aloud / certificate still work.
// ============================================================
window.renderResultsFromAPI = function (data) {
  const declarations   = _mapApiToDeclarations(data);
  const isCompliant    = data.pcr_2011 && data.pcr_2011.is_compliant;
  const violationCount = declarations.filter(d => d.status === 'violation').length;
  const compliantCount = declarations.filter(d => d.status === 'compliant').length;
  const total          = declarations.length;

  const productName = [data.company, data.quantity]
    .filter(Boolean).join(' x ') || 'Scanned product';

  // Populate global lastResult so share / read-aloud / certificate work
  lastResult = { productName, pass: isCompliant, declarations, violations: violationCount, compliant: compliantCount, total };

  // Score band
  const band = document.getElementById('scoreBand');
  if (band) {
    band.className = 'score-band ' + (isCompliant ? 'pass' : 'fail');
    if (typeof animateScoreCount === 'function') animateScoreCount(compliantCount, total);
    const scoreLabel = document.getElementById('scoreLabel');
    if (scoreLabel) scoreLabel.textContent = isCompliant
      ? 'all mandatory declarations compliant'
      : violationCount + ' declaration' + (violationCount !== 1 ? 's' : '') + ' non-compliant';
  }

  // Declaration rows
  const list = document.getElementById('declList');
  if (list) {
    list.innerHTML = declarations.map(function(d) {
      var badgeLabel = d.status === 'compliant' ? 'Compliant'
                     : d.status === 'violation'  ? 'Violation'
                     : 'Needs Review';
      var foundClass = d.status === 'violation' ? 'found empty' : 'found';
      return '<div class="decl-row">'
        + '<div class="name">' + _esc(d.name) + '</div>'
        + '<div class="' + foundClass + '">' + _esc(d.found) + '</div>'
        + '<div><span class="badge ' + d.status + '">' + badgeLabel + '</span></div>'
        + '</div>';
    }).join('');

    // OCR confidence note
    if (data.confidence !== undefined) {
      var lowConf = data.confidence < 55;
      list.innerHTML += '<div style="padding:10px 4px 0 4px;font-size:0.78rem;color:var(--muted);font-family:IBM Plex Mono,monospace;">'
        + 'OCR confidence: <b style="color:' + (lowConf ? 'var(--violation)' : 'var(--compliant)') + '">'
        + data.confidence + '%</b>'
        + (lowConf ? ' -- try a clearer, well-lit photo' : '')
        + '</div>';
    }

    // PCR 2011 violation summary
    var pcrViolations = (data.pcr_2011 && data.pcr_2011.violations) || [];
    if (pcrViolations.length) {
      list.innerHTML += '<div style="margin-top:14px;padding:12px 14px;background:var(--violation-bg);border-radius:4px;font-size:0.82rem;">'
        + '<b style="color:var(--violation);">PCR 2011 issues:</b>'
        + '<ul style="margin:6px 0 0 0;padding-left:18px;color:var(--violation);">'
        + pcrViolations.map(function(v){ return '<li>' + _esc(v) + '</li>'; }).join('')
        + '</ul></div>';
    }
  }

  // Verified panel
  var slot = document.getElementById('verifiedPanelSlot');
  if (slot) {
    slot.innerHTML = isCompliant
      ? '<div class="verified-panel">'
        + '<div class="verified-seal"><svg viewBox="0 0 24 24" fill="none"><path d="M5 12l4 4L19 6" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></div>'
        + '<div class="verified-text">'
        + '<div class="t">Eligible for Lablelens Verified</div>'
        + '<div class="d">This label passes every mandatory declaration under PCR 2011. A QR-linked Verified badge can be displayed on the packaging or e-commerce listing.</div>'
        + '</div></div>'
      : '';
  }

  // Save to local history
  if (typeof _saveToLocalHistory === 'function') {
    _saveToLocalHistory({ productName: productName, pass: isCompliant, confidence: data.confidence });
  }

  // Show / hide action buttons
  var compBtn = document.getElementById('complaintBtn');
  if (compBtn) compBtn.style.display = (currentRole === 'public' && !isCompliant) ? 'inline-flex' : 'none';
  var certBtn = document.getElementById('certificateBtn');
  if (certBtn) certBtn.style.display = isCompliant ? 'inline-flex' : 'none';
  var resSub = document.getElementById('resultsSub');
  if (resSub) {
    resSub.textContent = 'Checked against the Legal Metrology (Packaged Commodities) Rules, 2011'
      + (isCompliant ? '' : ' -- issues found below');
  }

  if (typeof updateOfficerSessionBar === 'function') updateOfficerSessionBar();

  // Also update the Six Declarations checklist on the inspection screen
  if (typeof renderSixDeclarationsFromAPI === 'function') {
    renderSixDeclarationsFromAPI(data);
  }
};

// ============================================================
// Map API JSON to declaration row objects
// ============================================================
function _mapApiToDeclarations(data) {
  var rows = [];
  var pcrViolations = (data.pcr_2011 && data.pcr_2011.violations) || [];

  // 1. Manufacturer / company
  var company = data.company;
  rows.push({
    name:   'Manufacturer name & address',
    found:  company || 'Not found on label',
    status: company ? 'compliant' : 'violation',
  });

  // 2. Net quantity + unit check
  var qty = data.quantity;
  var qtyDisplay = qty || 'Not found on label';
  var qtyStatus  = qty ? 'compliant' : 'violation';
  if (qty && data.unit_violation) {
    qtyStatus  = 'warn';
    qtyDisplay = qty + " -- '" + data.unit_violation + "' is non-standard under PCR 2011 (use SI units)";
  }
  rows.push({ name: 'Net quantity', found: qtyDisplay, status: qtyStatus });

  // 3. MRP + inclusive of all taxes
  var mrpDisplay = 'Not found on label';
  var mrpStatus  = 'violation';
  if (data.mrp) {
    mrpDisplay = 'Rs. ' + data.mrp.toFixed(2);
    if (data.inclusive_tax) {
      mrpDisplay += ' (incl. of all taxes)';
      mrpStatus   = 'compliant';
    } else {
      mrpDisplay += ' -- "inclusive of all taxes" not declared';
      mrpStatus   = 'warn';
    }
  }
  rows.push({ name: 'Maximum Retail Price', found: mrpDisplay, status: mrpStatus });

  // 4. Manufacturing date (mandatory under PCR 2011)
  var mfgDate = data.mfg_date;
  rows.push({
    name:   'Month & year of manufacture',
    found:  mfgDate ? _fmtDate(mfgDate) : 'Not found on label',
    status: mfgDate ? 'compliant' : 'violation',
  });

  // 5. Expiry / best-before (optional but checked)
  var expDate = data.exp_date;
  var expDisplay = 'Not stated (optional for most categories)';
  var expStatus  = 'compliant';
  if (expDate) {
    expDisplay = _fmtDate(expDate);
    var expExpired = pcrViolations.some(function(v){ return v.toLowerCase().indexOf('expir') >= 0; });
    if (expExpired) {
      expDisplay += ' -- may be expired';
      expStatus = 'warn';
    }
  }
  rows.push({
    name:   'Best before / expiry date',
    found:  expDisplay,
    status: expStatus,
  });

  // 6. Consumer care details
  var care = data.consumer_care;
  rows.push({
    name:   'Consumer care details',
    found:  care || 'Not found on label',
    status: care ? 'compliant' : 'warn',
  });

  // 7. FSSAI licence
  var fssai = data.fssai;
  rows.push({
    name:   'FSSAI licence number',
    found:  fssai || 'Not found (mandatory for food & beverage)',
    status: fssai ? 'compliant' : 'warn',
  });

  return rows;
}

// ============================================================
// POST image to FastAPI backend
// ============================================================
async function _callAnalyzeAPI(blob) {
  var formData = new FormData();
  formData.append('image', blob, 'label.jpg');

  var response;
  try {
    response = await fetch(API_BASE + '/api/analyze', {
      method: 'POST',
      body:   formData,
    });
  } catch (networkErr) {
    throw new Error(
      'Cannot reach the server at ' + API_BASE + '. ' +
      'Start the backend: uvicorn main:app --reload --port 8000'
    );
  }

  if (!response.ok) {
    var detail = 'Server error (HTTP ' + response.status + ')';
    try { detail = (await response.json()).detail || detail; } catch(_) {}
    throw new Error(detail);
  }

  return response.json();
}

// ============================================================
// Convert data URL to Blob
// ============================================================
function _dataUrlToBlob(dataUrl) {
  var parts  = dataUrl.split(',');
  var match  = parts[0].match(/:(.*?);/);
  var mime   = match ? match[1] : 'image/jpeg';
  var binary = atob(parts[1]);
  var bytes  = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

// ============================================================
// Show a friendly error on the scan screen
// ============================================================
function _handleAnalysisError(error) {
  console.error('[Tolmaap] Analysis failed:', error);

  var msg     = (error.message || '').toLowerCase();
  var userMsg = 'Could not read the label. Try a closer, well-lit photo.';

  if (msg.indexOf('server') >= 0 || msg.indexOf('reach') >= 0 || msg.indexOf('fetch') >= 0)
    userMsg = 'Server not reachable. Start it with: uvicorn main:app --reload --port 8000';
  else if (msg.indexOf('blurry') >= 0 || msg.indexOf('no text') >= 0)
    userMsg = 'Image too blurry. Move closer and retake.';
  else if (msg.indexOf('413') >= 0 || msg.indexOf('large') >= 0)
    userMsg = 'Image too large. Try a lower resolution photo.';
  else if (msg.indexOf('415') >= 0 || msg.indexOf('type') >= 0)
    userMsg = 'Unsupported format. Use JPEG or PNG.';

  showScreen('screen-scan');
  setStep('stepCapture');

  var capturedImg = document.getElementById('capturedImg');
  if (capturedImg.src && capturedImg.src !== window.location.href) {
    document.getElementById('controlsCaptured').style.display    = 'flex';
    document.getElementById('controlsLive').style.display         = 'none';
    document.getElementById('controlsPreCapture').style.display   = 'none';
  }

  var placeholder = document.getElementById('camPlaceholder');
  placeholder.style.display = 'block';
  placeholder.innerHTML =
    '<span class="state-icon" style="font-size:1.8rem;">&#9888;&#65039;</span>'
    + '<div style="margin-top:8px;max-width:260px;margin-left:auto;margin-right:auto;">' + userMsg + '</div>'
    + '<div style="margin-top:10px;font-size:0.75rem;opacity:0.7;">Tap "Analyze label" to retry, or "Retake" to take a new photo.</div>';
}

// ============================================================
// Save real scan to localStorage history
// ============================================================
function _saveToLocalHistory(scan) {
  try {
    var key    = 'tolmaap_history';
    var stored = JSON.parse(localStorage.getItem(key) || '[]');
    stored.unshift({
      name:       scan.productName,
      when:       'Just now',
      status:     scan.pass ? 'compliant' : 'violation',
      confidence: scan.confidence,
      initial:    (scan.productName || 'S').charAt(0).toUpperCase(),
      color:      scan.pass ? '#2F7A4D' : '#B3413A',
    });
    localStorage.setItem(key, JSON.stringify(stored.slice(0, 20)));
  } catch(_) {}
}

// ============================================================
// Utility helpers
// ============================================================
function _fmtDate(d) {
  if (!d) return '';
  if (typeof d === 'string') return d;
  if (d.display) return d.display;
  var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var mn = (typeof d.month_name === 'string' && isNaN(Number(d.month_name)))
    ? d.month_name.charAt(0).toUpperCase() + d.month_name.slice(1, 3).toLowerCase()
    : (MONTHS[(d.month || 1) - 1] || '');
  var base = d.day ? (d.day + ' ' + mn + ' ' + d.year) : (mn + (d.year ? (' ' + d.year) : ''));
  if (d.inferred_from_shelf_life && base) {
    return d.inferred_from_shelf_life + ' (' + base + ')';
  }
  return base || (d.shelf_life_statement || '');
}

function _esc(str) {
  return String(str || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ============================================================
// Silent health check on page load -- warns in console only
// ============================================================
(function _healthCheck() {
  fetch(API_BASE + '/health', { signal: AbortSignal.timeout(3000) })
    .then(function(r) {
      if (r.ok) console.info('[Tolmaap] Backend reachable at', API_BASE);
      else console.warn('[Tolmaap] Backend responded with status', r.status);
    })
    .catch(function() {
      console.warn('[Tolmaap] Backend not reachable at ' + API_BASE + '. Start it with: uvicorn main:app --reload --port 8000');
    });
})();
