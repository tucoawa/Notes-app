// Map note client — Leaflet initialization, tile layer, waypoint CRUD,
// view persistence, GPX rendering, and unified controls.

(function () {
  if (!window.IS_MAP_NOTE) return;

  const container = document.getElementById('map-container');
  if (!container) return;

  // ─── Helpers ─────────────────────────────────────────────────────────────

  function readMapView() {
    const p = window.NOTE_PROPS || {};
    if (p.map_view && typeof p.map_view === 'object'
        && typeof p.map_view.lat === 'number'
        && typeof p.map_view.lng === 'number'
        && typeof p.map_view.zoom === 'number') {
      return p.map_view;
    }
    return null;
  }

  function readPending() {
    const p = window.NOTE_PROPS || {};
    if (Array.isArray(p.map_pending)) {
      return p.map_pending.filter(pt =>
        pt && typeof pt.lat === 'number' && typeof pt.lng === 'number'
      );
    }
    return [];
  }

  function writePending(arr) {
    window.NOTE_PROPS = window.NOTE_PROPS || {};
    window.NOTE_PROPS.map_pending = arr;
    savePropertiesPatch({ map_pending: arr });
  }

  function writeMapView(view) {
    window.NOTE_PROPS = window.NOTE_PROPS || {};
    window.NOTE_PROPS.map_view = view;
    savePropertiesPatch({ map_view: view });
  }

  let saveDebounce = null;
  function savePropertiesPatch(patch) {
    clearTimeout(saveDebounce);
    saveDebounce = setTimeout(() => {
      const merged = { ...(window.NOTE_PROPS || {}) };
      const payload = {
        title: window.NOTE_TITLE || '',
        content: '',
        properties: merged,
        due_at: null,
        link_ids: [],
        _client_updated_at: window.NOTE_UPDATED_AT || '',
        _client_id: 'map-client',
      };
      fetch(`/note/${window.NOTE_ID}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data && data.updated_at) {
            window.NOTE_UPDATED_AT = data.updated_at;
          }
        })
        .catch(() => {});
    }, 400);
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // ─── Map init ────────────────────────────────────────────────────────────

  // Base layers
  const osmLayer = L.tileLayer('/tiles/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© OpenStreetMap contributors',
  });

  const satelliteLayer = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {
      maxZoom: 19,
      attribution: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics',
    }
  );

  const map = L.map(container, {
    zoomControl: true,
    attributionControl: true,
    tapTolerance: 12,
  });

  const savedView = readMapView();
  if (savedView) {
    map.setView([savedView.lat, savedView.lng], savedView.zoom);
  } else {
    const def = window.MAP_DEFAULT || { lat: -23.2208, lng: -44.7136, zoom: 13 };
    map.setView([def.lat, def.lng], def.zoom);
  }

  osmLayer.addTo(map);

  // Handle focus_point URL param
  (function handleFocusPoint() {
    const params = new URLSearchParams(location.search);
    const fp = params.get('focus_point');
    if (!fp) return;
    const parts = fp.split(',');
    if (parts.length !== 2) return;
    const lat = parseFloat(parts[0]);
    const lng = parseFloat(parts[1]);
    if (!isFinite(lat) || !isFinite(lng)) return;

    setTimeout(() => {
      map.flyTo([lat, lng], 18, { duration: 0.8 });
    }, 300);

    params.delete('focus_point');
    const newSearch = params.toString();
    const newUrl = location.pathname + (newSearch ? '?' + newSearch : '');
    history.replaceState(null, '', newUrl);
  })();

  // ─── State ───────────────────────────────────────────────────────────────

  let currentBaseLayer = 'map';
  
  let followMode = false;
  let followWatchId = null;
  let followMarker = null;
  let followCircle = null;

  const loadedTracks = new Map();

  // ─── Track Helpers ───────────────────────────────────────────────────────

  function getHiddenTracks() {
    const p = window.NOTE_PROPS || {};
    return Array.isArray(p.hidden_tracks) ? p.hidden_tracks : [];
  }

  function setHiddenTracks(urls) {
    window.NOTE_PROPS = window.NOTE_PROPS || {};
    window.NOTE_PROPS.hidden_tracks = urls;
    savePropertiesPatch({ hidden_tracks: urls });
  }

  function isTrackHidden(url) {
    return getHiddenTracks().indexOf(url) !== -1;
  }

  function trackLabelFor(url) {
    const attachments = window.MAP_GPX_ATTACHMENTS || [];
    const rec = attachments.find(a => a.url === url);
    if (rec && rec.original_name) return rec.original_name;
    return url.split('/').pop();
  }

  function toggleTrack(url) {
    const entry = loadedTracks.get(url);
    if (!entry) return;
    const hidden = getHiddenTracks();
    const wasHidden = hidden.indexOf(url) !== -1;
    const newHidden = wasHidden ? hidden.filter(u => u !== url) : hidden.concat([url]);
    setHiddenTracks(newHidden);
    
    if (wasHidden) {
      if (entry.gpx && !map.hasLayer(entry.gpx)) entry.gpx.addTo(map);
    } else {
      if (entry.gpx && map.hasLayer(entry.gpx)) map.removeLayer(entry.gpx);
    }
    renderTracksPopup();
  }

  function renderGpx(url, fitView = true) {
    if (typeof L.GPX !== 'function') {
      console.warn('leaflet-gpx not loaded');
      return;
    }
    if (loadedTracks.has(url)) return;

    const label = trackLabelFor(url);
    const hidden = isTrackHidden(url);

    const gpx = new L.GPX(url, {
      async: true,
      marker_options: { startIconUrl: null, endIconUrl: null, shadowUrl: null },
      polyline_options: {
        color: '#ff6b35', weight: 4, opacity: 0.95,
        lineCap: 'round', lineJoin: 'round',
      },
    });

    loadedTracks.set(url, { gpx, label, url });

    gpx.on('loaded', () => {
      if (!isTrackHidden(url)) {
        gpx.addTo(map);
        // Only auto-zoom if fitView is true (e.g. loading the page, or manual import)
        if (fitView) {
          map.fitBounds(gpx.getBounds(), { padding: [24, 24] });
        }
      }
      if (typeof renderTracksPopup === 'function') renderTracksPopup();
    });
    gpx.on('error', (e) => console.warn('GPX parse error', e));
  }

  // ─── Follow-me Logic ─────────────────────────────────────────────────────

  function stopFollowing() {
    followMode = false;
    if (followBtn) followBtn.classList.remove('active');
    if (followWatchId !== null) {
      navigator.geolocation.clearWatch(followWatchId);
      followWatchId = null;
    }
    if (followMarker) { map.removeLayer(followMarker); followMarker = null; }
    if (followCircle) { map.removeLayer(followCircle); followCircle = null; }
  }
  
  // ─── GPX Tracking state ──────────────────────────────────────────────────
  let trackMode = false;
  let trackWatchId = null;
  let trackPoints = [];
  let trackPolyline = null;
  let trackStartTime = null;
  let lastTrackPoint = null;

  function startTracking() {
    if (!navigator.geolocation) { alert('Geolocation not supported'); return; }
    trackMode = true;
    trackPoints = [];
    trackStartTime = new Date();
    lastTrackPoint = null;
    
    if (recordBtn) {
      recordBtn.classList.add('recording');
      recordBtn.style.background = '#ff3b30'; // Red background
      const icon = recordBtn.querySelector('.material-symbols-rounded');
      if (icon) {
        icon.style.setProperty('color', '#ff3b30', 'important'); // Solid red icon
      }
    }

    trackPolyline = L.polyline([], {
      color: '#ff3b30',
      weight: 5,
      opacity: 0.9,
      lineCap: 'round',
      lineJoin: 'round',
    }).addTo(map);

    // Add a visible red dot at the current position to make it obvious we are recording
    if (window.__trackLiveDot) map.removeLayer(window.__trackLiveDot);
    window.__trackLiveDot = L.circleMarker([0, 0], {
      radius: 8,
      fillColor: '#ff3b30',
      fillOpacity: 1,
      color: '#ffffff',
      weight: 3,
      interactive: false
    }).addTo(map);

    trackWatchId = navigator.geolocation.watchPosition(
      (pos) => {
        if (!trackMode) return;
        
        const lat = pos.coords.latitude;
        const lng = pos.coords.longitude;
        const acc = pos.coords.accuracy || 0;
        const alt = pos.coords.altitude || null;

        // Ignore points with terrible accuracy (> 30m)
        if (acc > 30) return;

        // Ignore points that are too close to the last one (< 5 meters)
        // to avoid spamming points while standing still.
        if (lastTrackPoint) {
          const dist = map.distance([lastTrackPoint.lat, lastTrackPoint.lng], [lat, lng]);
          if (dist < 5) return;
        }

        const newPoint = { lat, lng, alt, time: new Date() };
        trackPoints.push(newPoint);
        lastTrackPoint = newPoint;

        // Update the live polyline
        trackPolyline.addLatLng([lat, lng]);

        // Move the live dot to the new position
        if (window.__trackLiveDot) {
          window.__trackLiveDot.setLatLng([lat, lng]);
        }

        // Keep the map following the user while recording
        map.setView([lat, lng], map.getZoom(), { animate: true });
      },
      (err) => {
        console.warn('[tracking] geolocation error', err);
        stopTracking();
      },
      { enableHighAccuracy: true, watch: true, maximumAge: 0, timeout: 15000 }
    );
  }

  async function stopTracking() {
    if (!trackMode) return;
    trackMode = false;
    if (recordBtn) {
      recordBtn.classList.remove('recording');
      recordBtn.style.background = ''; // Revert to normal dark background
      const icon = recordBtn.querySelector('.material-symbols-rounded');
      if (icon) {
        icon.style.setProperty('color', '#ffffff', 'important'); // Back to white
      }
    }

    if (trackWatchId !== null) {
      navigator.geolocation.clearWatch(trackWatchId);
      trackWatchId = null;
    }

    const points = trackPoints;
    const startTime = trackStartTime;
    
    // Reset state
    trackPoints = [];
    trackStartTime = null;
    lastTrackPoint = null;

    if (trackPolyline) {
      map.removeLayer(trackPolyline);
      trackPolyline = null;
    }
    if (window.__trackLiveDot) {
      map.removeLayer(window.__trackLiveDot);
      window.__trackLiveDot = null;
    }

    if (points.length < 2) {
      alert('Track too short to save.');
      return;
    }

    // Generate default name using LOCAL time, not UTC
    const pad = n => String(n).padStart(2, '0');
    const dateStr = `${startTime.getFullYear()}-${pad(startTime.getMonth()+1)}-${pad(startTime.getDate())} ${pad(startTime.getHours())}-${pad(startTime.getMinutes())}`;
    const defaultName = `Track ${dateStr}`;

    // Custom dialog to avoid keyboard issues
    const fileName = await new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'map-naming-modal';
      overlay.innerHTML = `
        <div class="map-naming-backdrop"></div>
        <div class="map-naming-sheet">
          <h3>Save GPX Track</h3>
          <input type="text" class="map-naming-input" value="${defaultName}" autofocus>
          <div class="map-naming-actions">
            <button type="button" class="btn map-naming-cancel">Cancel</button>
            <button type="button" class="btn primary map-naming-ok">Save</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);

      const input = overlay.querySelector('.map-naming-input');
      setTimeout(() => {
        input.focus();
        input.select();
      }, 60);

      function close(val) {
        overlay.remove();
        resolve(val);
      }

      overlay.querySelector('.map-naming-backdrop').addEventListener('click', () => close(null));
      overlay.querySelector('.map-naming-cancel').addEventListener('click', () => close(null));
      overlay.querySelector('.map-naming-ok').addEventListener('click', () => close(input.value.trim()));
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') close(input.value.trim());
        if (e.key === 'Escape') close(null);
      });
    });

    if (!fileName) return;
    
    let safeName = fileName;
    if (!safeName.toLowerCase().endsWith('.gpx')) {
      safeName += '.gpx';
    }

    // Generate GPX XML
    let gpxStr = `<?xml version="1.0" encoding="UTF-8"?>\n`;
    gpxStr += `<gpx version="1.1" creator="Notes App" xmlns="http://www.topografix.com/GPX/1/1">\n`;
    gpxStr += `  <trk>\n    <name>${escapeHtml(safeName)}</name>\n    <trkseg>\n`;

    points.forEach(pt => {
      gpxStr += `      <trkpt lat="${pt.lat}" lon="${pt.lng}">\n`;
      if (pt.alt !== null) {
        gpxStr += `        <ele>${pt.alt}</ele>\n`;
      }
      gpxStr += `        <time>${pt.time.toISOString()}</time>\n`;
      gpxStr += `      </trkpt>\n`;
    });

    gpxStr += `    </trkseg>\n  </trk>\n</gpx>\n`;

    // Upload to server
    try {
      const blob = new Blob([gpxStr], { type: 'application/gpx+xml' });
      const fd = new FormData();
      fd.append('file', blob, safeName);

      const r = await fetch(`/note/${window.NOTE_ID}/upload`, {
        method: 'POST',
        body: fd,
      });

      if (!r.ok) throw new Error('Upload failed');
      const data = await r.json();
      
      if (data.files && data.files[0]) {
        const newUrl = data.files[0].url;
        // Add to loaded tracks and render (passing `false` to prevent fitBounds)
        renderGpx(newUrl, false);
        
        // Show toast instead of alert
        if (typeof window.showToast === 'function') {
          window.showToast(`Saved: ${safeName}`);
        } else {
          alert(`Track saved as ${safeName}`);
        }
      }
    } catch (err) {
      alert('Failed to save track: ' + err.message);
    }
  }

  function startFollowing() {
    if (!navigator.geolocation) { alert('Geolocation not supported'); return; }
    followMode = true;
    if (followBtn) followBtn.classList.add('active');

    // Clear any leftover one-shot marker so we don't double up
    if (window.__oneShotMarker) { map.removeLayer(window.__oneShotMarker); window.__oneShotMarker = null; }
    if (window.__oneShotCircle) { map.removeLayer(window.__oneShotCircle); window.__oneShotCircle = null; }

    followWatchId = navigator.geolocation.watchPosition(
      (pos) => {
        const lat = pos.coords.latitude;
        const lng = pos.coords.longitude;
        const acc = pos.coords.accuracy || 0;

        if (!followMarker) {
          followMarker = L.circleMarker([lat, lng], {
            radius: 8,
            color: '#ffffff',
            weight: 3,
            fillColor: '#ff3b30',
            fillOpacity: 1,
          }).addTo(map);
          followCircle = L.circle([lat, lng], {
            radius: acc,
            color: '#ff3b30',
            weight: 1,
            fillColor: '#ff3b30',
            fillOpacity: 0.15,
            interactive: false,
          }).addTo(map);
        } else {
          followMarker.setLatLng([lat, lng]);
          followCircle.setLatLng([lat, lng]).setRadius(acc);
        }
        map.setView([lat, lng], map.getZoom(), { animate: true });
      },
      (err) => { console.warn('[follow-me]', err); stopFollowing(); },
      { enableHighAccuracy: true, watch: true, maximumAge: 3000, timeout: 20000 }
    );
  }

  // ─── One-shot Locate Logic ───────────────────────────────────────────────

  function oneShotLocate() {
    if (!navigator.geolocation) { alert('Geolocation not supported'); return; }
    if (locateBtn) locateBtn.classList.add('active');
    
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (locateBtn) locateBtn.classList.remove('active');
        const { latitude, longitude } = pos.coords;
        
        if (window.__oneShotMarker) map.removeLayer(window.__oneShotMarker);
        if (window.__oneShotCircle) map.removeLayer(window.__oneShotCircle);
        
        window.__oneShotMarker = L.circleMarker([latitude, longitude], {
          radius: 8, color: '#fff', weight: 3,
          fillColor: '#ff3b30', fillOpacity: 1,
        }).addTo(map);
        
        window.__oneShotCircle = L.circle([latitude, longitude], {
          radius: pos.coords.accuracy || 0, color: '#ff3b30', weight: 1,
          fillColor: '#ff3b30', fillOpacity: 0.15, interactive: false,
        }).addTo(map);
        
        map.setView([latitude, longitude], map.getZoom(), { animate: true });
      },
      (err) => {
        if (locateBtn) locateBtn.classList.remove('active');
        console.warn('[locate]', err);
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
    );
  }

  // ─── Unified right-side control stack ────────────────────────────────────

  const stackEl = document.createElement('div');
  stackEl.className = 'map-control-stack';

  function makeBtn(icon, title) {
    const a = document.createElement('a');
    a.href = '#';
    a.className = 'map-control-btn';
    a.title = title;
    a.innerHTML = '<span class="material-symbols-rounded">' + icon + '</span>';
    L.DomEvent.disableClickPropagation(a);
    L.DomEvent.on(a, 'click', L.DomEvent.stop);
    return a;
  }

  const locateBtn = makeBtn('my_location', 'Show my location');
  const followBtn = makeBtn('near_me', 'Follow my location');
  const layersBtn = makeBtn('layers', 'Map / Satellite');
  const tracksBtn = makeBtn('route', 'Tracks');
  const recordBtn = makeBtn('fiber_manual_record', 'Record GPX Track');

  // Default state: solid white icon
  const recordIcon = recordBtn.querySelector('.material-symbols-rounded');
  if (recordIcon) {
    recordIcon.style.setProperty('color', '#ffffff', 'important');
  }

  stackEl.appendChild(locateBtn);
  stackEl.appendChild(followBtn);
  stackEl.appendChild(layersBtn);
  stackEl.appendChild(tracksBtn);
  stackEl.appendChild(recordBtn);

  const stackControl = L.control({ position: 'topright' });
  stackControl.onAdd = () => stackEl;
  stackControl.addTo(map);

  // ─── Popup Logic ─────────────────────────────────────────────────────────

  let layersPopup = null;
  let tracksPopup = null;

  function closeAllPopups() {
    if (layersPopup) { layersPopup.remove(); layersPopup = null; }
    if (tracksPopup) { tracksPopup.remove(); tracksPopup = null; }
    layersBtn.classList.remove('active');
    tracksBtn.classList.remove('active');
  }
  
  map.on('click', () => closeAllPopups());

  function positionPopupNear(popup, anchor) {
    const r = anchor.getBoundingClientRect();
    popup.style.position = 'fixed';
    popup.style.top = r.top + 'px';
    popup.style.right = (window.innerWidth - r.left + 8) + 'px';
    popup.style.left = 'auto';
    popup.style.zIndex = '900';
  }

  // ─── Wire Buttons ────────────────────────────────────────────────────────

  locateBtn.addEventListener('click', (e) => { e.preventDefault(); oneShotLocate(); });

  followBtn.addEventListener('click', (e) => {
    e.preventDefault();
    if (followMode) stopFollowing(); else startFollowing();
  });

  layersBtn.addEventListener('click', (e) => {
    e.preventDefault();
    if (layersPopup) { closeAllPopups(); return; }
    closeAllPopups();
    
    layersPopup = document.createElement('div');
    layersPopup.className = 'map-control-popup';
    layersPopup.innerHTML = `
      <label><input type="radio" name="base-layer" value="map" ${currentBaseLayer === 'map' ? 'checked' : ''}> Map</label>
      <label><input type="radio" name="base-layer" value="satellite" ${currentBaseLayer === 'satellite' ? 'checked' : ''}> Satellite</label>
    `;
    layersPopup.addEventListener('change', (ev) => {
      const v = ev.target.value;
      if (v === 'map') {
        if (map.hasLayer(satelliteLayer)) map.removeLayer(satelliteLayer);
        if (!map.hasLayer(osmLayer)) osmLayer.addTo(map);
        currentBaseLayer = 'map';
      } else {
        if (map.hasLayer(osmLayer)) map.removeLayer(osmLayer);
        if (!map.hasLayer(satelliteLayer)) satelliteLayer.addTo(map);
        currentBaseLayer = 'satellite';
      }
    });
    document.body.appendChild(layersPopup);
    positionPopupNear(layersPopup, layersBtn);
    layersBtn.classList.add('active');
  });

  tracksBtn.addEventListener('click', (e) => {
    e.preventDefault();
    if (tracksPopup) { closeAllPopups(); return; }
    closeAllPopups();
    
    tracksPopup = document.createElement('div');
    tracksPopup.className = 'map-control-popup map-tracks-popup';
    document.body.appendChild(tracksPopup);
    renderTracksPopup();
    positionPopupNear(tracksPopup, tracksBtn);
    tracksBtn.classList.add('active');
  });
  
  // Wire record button
  recordBtn.addEventListener('click', (e) => {
    e.preventDefault();
    if (trackMode) {
      stopTracking();
    } else {
      startTracking();
    }
  });  

  function renderTracksPopup() {
    if (!tracksPopup) return;
    const hidden = getHiddenTracks();
    let html = '<div class="map-tracks-popup-title">' +
      '<span class="material-symbols-rounded">route</span>' +
      ' Tracks (' + loadedTracks.size + ')</div>';
      
    if (loadedTracks.size === 0) {
      html += '<div class="map-tracks-empty">No tracks on this note.</div>';
    } else {
      loadedTracks.forEach((entry, url) => {
        const isH = hidden.indexOf(url) !== -1;
        html +=
          '<div class="map-tracks-row">' +
            '<span class="map-tracks-name" title="' + escapeHtml(entry.label) + '">' +
              escapeHtml(entry.label) +
            '</span>' +
            '<button type="button" class="map-tracks-eye' + (isH ? ' hidden' : '') + '" ' +
              'data-track-url="' + escapeHtml(url) + '" ' +
              'title="' + (isH ? 'Show' : 'Hide') + '">' +
              '<span class="material-symbols-rounded">' +
                (isH ? 'visibility_off' : 'visibility') +
              '</span>' +
            '</button>' +
          '</div>';
      });
    }
    tracksPopup.innerHTML = html;
    tracksPopup.querySelectorAll('.map-tracks-eye').forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        toggleTrack(btn.dataset.trackUrl);
      });
    });
  }

  // ─── GPX Upload Button ───────────────────────────────────────────────────

  const gpxBtn = document.getElementById('import-gpx-btn');
  if (gpxBtn) {
    gpxBtn.addEventListener('click', () => {
      const fileInput = document.createElement('input');
      fileInput.type = 'file';
      fileInput.accept = '.gpx,application/gpx+xml';
      fileInput.addEventListener('change', async () => {
        if (!fileInput.files.length) return;
        const f = fileInput.files[0];
        const fd = new FormData();
        fd.append('file', f);
        try {
          const r = await fetch(`/note/${window.NOTE_ID}/upload`, {
            method: 'POST',
            body: fd,
          });
          if (!r.ok) {
            alert('GPX upload failed');
            return;
          }
          const data = await r.json();
          if (data.files && data.files[0]) {
            renderGpx(data.files[0].url);
          }
        } catch (e) {
          alert('GPX upload failed: ' + e.message);
        }
      });
      fileInput.click();
    });
  }

  // ─── View persistence ────────────────────────────────────────────────────

  let userInteracted = false;
  map.on('dragstart', () => { userInteracted = true; });
  map.on('zoomstart', (e) => {
    if (e && e.originalEvent) userInteracted = true;
  });

  map.on('moveend zoomend', () => {
    if (!userInteracted) return;
    const c = map.getCenter();
    writeMapView({ lat: c.lat, lng: c.lng, zoom: map.getZoom() });
  });

  // ─── Waypoint Icons ──────────────────────────────────────────────────────

  const pendingIcon = L.divIcon({
    className: 'map-waypoint map-waypoint-pending',
    html: '<span class="map-waypoint-hit"><span class="map-waypoint-pin"></span></span>',
    iconSize: [40, 40],
    iconAnchor: [20, 20],
  });

  function namedIcon(title) {
    return L.divIcon({
      className: 'map-waypoint map-waypoint-named',
      html:
        '<span class="map-waypoint-hit">' +
        '<span class="map-waypoint-pin"></span>' +
        '</span>' +
        '<span class="map-waypoint-label">' +
        escapeHtml(title) +
        '</span>',
      iconSize: [40, 40],
      iconAnchor: [20, 20],
      labelAnchor: [0, 16],
    });
  }

  // ─── Waypoint Rendering ──────────────────────────────────────────────────

  let defineMode = false;
  let selectedPendingIdx = -1;
  let pendingMarkers = [];
  let namedMarkers = [];

  function renderPending() {
    pendingMarkers.forEach(pm => {
      if (pm.xBtn) pm.xBtn.remove();
      if (pm.marker) map.removeLayer(pm.marker);
    });
    pendingMarkers = [];

    const pending = readPending();
    pending.forEach((pt, i) => {
      const m = L.marker([pt.lat, pt.lng], {
        icon: pendingIcon,
        keyboard: false,
      }).addTo(map);
      m._pendingIdx = i;
      m._lat = pt.lat;
      m._lng = pt.lng;

      let xBtn = null;
      if (defineMode) {
        const offsetLatLng = L.latLng(pt.lat + 0.00002, pt.lng + 0.00004);
        const xIcon = L.divIcon({
          className: 'map-waypoint-x-wrap',
          html: '<button type="button" class="map-waypoint-x-btn" title="Remove this pending waypoint">×</button>',
          iconSize: [28, 28],
          iconAnchor: [14, 14],
        });
        xBtn = L.marker(offsetLatLng, {
          icon: xIcon,
          keyboard: false,
          riseOnHover: true,
          zIndexOffset: 1000,
        }).addTo(map);

        xBtn.on('mousedown', (e) => { if (e.originalEvent) e.originalEvent._stopped = true; });
        xBtn.on('click', (e) => {
          if (e.originalEvent) {
            e.originalEvent._stopped = true;
            L.DomEvent.stopPropagation(e.originalEvent);
          }
          map.removeLayer(xBtn);
          removePendingAt(i);
        });
      }

      m.on('click', (e) => {
        if (e.originalEvent) {
          e.originalEvent._stopped = true;
          L.DomEvent.stopPropagation(e.originalEvent);
        }
        openNamingModal(pt.lat, pt.lng, i);
      });

      pendingMarkers.push({ marker: m, lat: pt.lat, lng: pt.lng, xBtn });
    });
  }

  function renderNamed() {
    namedMarkers.forEach(nm => map.removeLayer(nm.marker));
    namedMarkers = [];

    fetch(`/note/${window.NOTE_ID}/children`, { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !data.children) return;
        data.children.forEach(child => {
          if (!child.properties) return;
          const lat = parseFloat(child.properties.latitude);
          const lng = parseFloat(child.properties.longitude);
          if (!isFinite(lat) || !isFinite(lng)) return;

          const m = L.marker([lat, lng], {
            icon: namedIcon(child.title),
            keyboard: false,
            riseOnHover: true,
          }).addTo(map);
          
          m._noteId = child.id;
          m.on('click', (e) => {
            L.DomEvent.stopPropagation(e);
            if (e.originalEvent) e.originalEvent._stopped = true;
            window.location.href = `/?note=${child.id}`;
          });
          m.on('add', () => {
            const el = m.getElement();
            if (!el) return;
            L.DomEvent.disableClickPropagation(el);
          });
          namedMarkers.push({ marker: m, lat, lng, note_id: child.id, title: child.title });
        });
      });
  }

  renderPending();
  renderNamed();

  // ─── Waypoint Interaction ────────────────────────────────────────────────

  map.on('click', (e) => {
    if (!defineMode) return;
    if (e.originalEvent && e.originalEvent._stopped) return;
    const lat = e.latlng.lat;
    const lng = e.latlng.lng;
    const pending = readPending();
    pending.push({ lat, lng });
    writePending(pending);
    renderPending();
  });

  const defineBtn = document.getElementById('define-waypoints-btn');
  if (defineBtn) {
    defineBtn.addEventListener('click', () => {
      defineMode = !defineMode;
      defineBtn.classList.toggle('primary', defineMode);
      renderPending();
    });
  }

  let namingOverlay = null;
  function openNamingModal(lat, lng, pendingIdx) {
    if (namingOverlay) return;

    namingOverlay = document.createElement('div');
    namingOverlay.className = 'map-naming-modal';
    namingOverlay.innerHTML = `
      <div class="map-naming-backdrop"></div>
      <div class="map-naming-sheet">
        <h3>Name this point</h3>
        <input type="text" class="map-naming-input" placeholder="Point name" autofocus>
        <textarea class="map-naming-body" placeholder="Optional body (markdown)…" rows="3"></textarea>
        <div class="map-naming-actions">
          <button type="button" class="btn map-naming-cancel">Cancel</button>
          <button type="button" class="btn primary map-naming-ok">Create</button>
        </div>
      </div>
    `;
    document.body.appendChild(namingOverlay);

    const input = namingOverlay.querySelector('.map-naming-input');
    const body = namingOverlay.querySelector('.map-naming-body');
    setTimeout(() => input.focus(), 60);

    function close() {
      namingOverlay.remove();
      namingOverlay = null;
    }

    namingOverlay.querySelector('.map-naming-backdrop').addEventListener('click', close);
    namingOverlay.querySelector('.map-naming-cancel').addEventListener('click', close);
    namingOverlay.querySelector('.map-naming-ok').addEventListener('click', async () => {
      const title = input.value.trim() || `Point ${new Date().toISOString().slice(0,16).replace('T',' ')}`;
      const text = body.value.trim();

      const r = await fetch(`/api/note/${window.NOTE_ID}/add-waypoint`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, lat, lng }),
      });
      if (!r.ok) {
        alert('Could not create waypoint note.');
        return;
      }
      const data = await r.json();

      if (text && data.id) {
        try {
          await fetch(`/note/${data.id}/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              title: data.title,
              content: text,
              properties: { latitude: lat.toFixed(6), longitude: lng.toFixed(6) },
              due_at: null,
              link_ids: [],
              _client_id: 'map-client',
            }),
          });
        } catch (e) {}
      }

      const pending = readPending();
      pending.splice(pendingIdx, 1);
      writePending(pending);
      close();
      renderPending();
      renderNamed();
    });
  }

  function removePendingAt(idx) {
    const pending = readPending();
    pending.splice(idx, 1);
    writePending(pending);
    renderPending();
  }

  // Auto-render existing GPX
  (function autoRenderExistingGpx() {
    const urls = window.MAP_GPX_URLS || [];
    urls.forEach(renderGpx);
  })();

  // Expose for other scripts
  window.__mapInstance = map;
  window.__mapRenderNamed = renderNamed;
  window.__mapRenderPending = renderPending;
})();