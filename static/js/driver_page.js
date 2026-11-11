/* ==========================================================================
   Driver trip page (/trip/driver/<token>/) — Alpine component `driverPage()`.

   Reads its one JSON blob (#driver-data: state, stops, map_key, route, csrf, urls) and
   keeps the server-rendered page in step with every POST: after any response that
   carries `state`, that state replaces ours. A 410 means the link died under us, so
   the page reloads into the dead view. Nothing is queued: a failed post says
   "No signal — tap again" and waits for the driver.

   Location: watchPosition only while the trip shares location and the page is visible;
   a ping goes out at most every 30 s or after 100 m, fire-and-forget. The map is
   MapLibre (loaded only when there is a public key) and its failures stay inside it:
   the stop list and the buttons never depend on it.

   Loaded without `defer` so `driverPage` exists before Alpine starts.
   ========================================================================== */
(function () {
  "use strict";

  var NO_SIGNAL = "No signal — tap again";
  var PING_EVERY_MS = 30000;
  var PING_METERS = 100;
  var POST_TIMEOUT_MS = 15000;
  var MAX_OPEN_WAIT_MS = 24 * 60 * 60 * 1000;
  // Statuses whose next stop is the final drop-off; before these it is the pickup.
  var DROP_OFF_NEXT = ["customer_in_car", "done"];
  var STYLE_URL = "https://tiles.locationiq.com/v3/streets/vector.json?key=";

  /* Google encoded polyline, precision 5 → [[lat, lng], …]. Stops at malformed input. */
  function decodePolyline(str) {
    var points = [];
    var index = 0;
    var lat = 0;
    var lng = 0;
    function next() {
      var result = 0;
      var shift = 0;
      var b;
      do {
        if (index >= str.length) return null;
        b = str.charCodeAt(index++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      return result & 1 ? ~(result >> 1) : result >> 1;
    }
    while (index < str.length) {
      var dLat = next();
      var dLng = next();
      if (dLat === null || dLng === null) break;
      lat += dLat;
      lng += dLng;
      points.push([lat / 1e5, lng / 1e5]);
    }
    return points;
  }

  /* Great-circle distance in metres. */
  function haversine(a, b) {
    var R = 6371000;
    var rad = Math.PI / 180;
    var dLat = (b.lat - a.lat) * rad;
    var dLng = (b.lng - a.lng) * rad;
    var h =
      Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function finite(v) {
    return typeof v === "number" && isFinite(v);
  }

  function readConfig() {
    var el = document.getElementById("driver-data");
    return el ? JSON.parse(el.textContent) : null;
  }

  function stopPoint(stop) {
    var lat = parseFloat(stop.lat);
    var lng = parseFloat(stop.lng);
    return isFinite(lat) && isFinite(lng) ? [lng, lat] : null;
  }

  function boundsOf(coords) {
    var minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    coords.forEach(function (c) {
      minLng = Math.min(minLng, c[0]);
      maxLng = Math.max(maxLng, c[0]);
      minLat = Math.min(minLat, c[1]);
      maxLat = Math.max(maxLat, c[1]);
    });
    return [[minLng, minLat], [maxLng, maxLat]];
  }

  function stopPin(n) {
    var el = document.createElement("div");
    el.className =
      "grid h-6 w-6 place-items-center rounded-full border-2 border-white bg-onyx text-xs font-bold text-gold shadow";
    el.setAttribute("aria-hidden", "true");
    el.textContent = String(n);
    return el;
  }

  function driverDot() {
    var el = document.createElement("div");
    el.className = "h-4 w-4 rounded-full border-2 border-white bg-sky-600 shadow";
    el.setAttribute("aria-hidden", "true");
    return el;
  }

  function driverPage() {
    var cfg = readConfig() || { state: {}, stops: [], urls: {} };
    // Kept out of Alpine's reactive proxy: a MapLibre map and a watch id are not UI state.
    var geo = { watchId: null, last: null, sent: null, sentAt: 0 };
    var map = null;
    var mapBounds = null;

    // Measure the real container, then fit: MapLibre sizes itself at construction, which can
    // be before layout (a 400x300 canvas in a 224 px frame), and the fit would be for that.
    function fitMap() {
      if (!map || !mapBounds) return;
      try {
        map.resize();
        map.fitBounds(mapBounds, { padding: 40, maxZoom: 15, animate: false });
      } catch (e) { /* the stop list stands alone */ }
    }
    var dot = null;

    return {
      state: cfg.state,
      stops: cfg.stops || [],
      tab: "route",
      busy: false,
      notice: "",
      sharing: false,
      locOff: false,
      mapOn: false,
      mapFailed: false,

      init: function () {
        var self = this;
        this.syncWatch();
        document.addEventListener("visibilitychange", function () {
          self.syncWatch();
          // Back on screen before the window: reload for fresh state (no reliance on the
          // phone's clock, which may be off).
          if (document.visibilityState === "visible" && self.waitingForWindow()) {
            window.location.reload();
          }
        });
        this.scheduleOpen();
        this.startMap();
      },

      /* ---- the 6-hour window -------------------------------------------- */
      // Only a POST changes `window_open`, so a page opened early reloads itself once the
      // window opens: on a timer (capped at 24 h) and whenever the phone shows it again.
      waitingForWindow: function () {
        return !this.state.window_open && !!this.state.next && !!this.state.opens_at;
      },

      reloadIfOpen: function () {
        var opens = Date.parse(this.state.opens_at);
        if (isFinite(opens) && Date.now() >= opens) window.location.reload();
      },

      scheduleOpen: function () {
        var self = this;
        if (!this.waitingForWindow()) return;
        var wait = Date.parse(this.state.opens_at) - Date.now();
        if (!isFinite(wait) || wait > MAX_OPEN_WAIT_MS) return;
        // A phone clock ahead of the server would reload every second: once per opens_at.
        var key = "driverOpenReload:" + this.state.opens_at;
        try { if (sessionStorage.getItem(key)) return; } catch (e) { /* storage unavailable */ }
        setTimeout(function () {
          try { sessionStorage.setItem(key, "1"); } catch (e) { /* storage unavailable */ }
          self.reloadIfOpen();
        }, Math.max(0, wait) + 1000);
      },

      get dropOffNext() {
        return DROP_OFF_NEXT.indexOf(this.state.status) >= 0;
      },

      get opensLine() {
        return "Opens at " + this.state.opens_at_label + ", 6 hours before pickup";
      },

      reached: function (step) {
        return this.state.step >= step;
      },

      showTab: function (name) {
        this.tab = name;
        if (name === "route" && map) {
          this.$nextTick(function () { fitMap(); });
        }
      },

      /* ---- posts ------------------------------------------------------- */
      send: async function (url, body) {
        var ctl = new AbortController();
        var timer = setTimeout(function () { ctl.abort(); }, POST_TIMEOUT_MS);
        try {
          var res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": cfg.csrf },
            body: JSON.stringify(body),
            credentials: "same-origin",
            // The page is served `no-referrer`, which makes a POST say `Origin: null` and
            // fail CSRF; same-origin sends our own origin and still nothing to third parties.
            referrerPolicy: "same-origin",
            signal: ctl.signal,
          });
          if (res.status === 410) {
            window.location.reload();
            return false;
          }
          var data = null;
          try { data = await res.json(); } catch (e) { data = null; }
          if (data && data.state) {
            this.applyState(data.state);
            // A refused tap (409/400) is explained by the state it returned.
            this.notice = "";
            return res.ok;
          }
          this.notice = res.ok ? "" : (data && data.error) || NO_SIGNAL;
          return res.ok;
        } catch (e) {
          this.notice = NO_SIGNAL;
          return false;
        } finally {
          clearTimeout(timer);
        }
      },

      applyState: function (next) {
        this.state = next;
        this.syncWatch();
      },

      setStatus: async function (to) {
        if (this.busy || !to) return false;
        this.busy = true;
        var body = { to: to, from: this.state.status };
        if (geo.last) {
          body.lat = geo.last.lat;
          body.lng = geo.last.lng;
          if (finite(geo.last.accuracy)) body.accuracy = geo.last.accuracy;
        }
        try {
          return await this.send(cfg.urls.status, body);
        } finally {
          this.busy = false;
        }
      },

      advance: function () {
        var self = this;
        var next = this.state.next;
        if (!next || this.busy || !this.state.window_open) return;
        if (next.to !== "done") {
          this.setStatus(next.to);
          return;
        }
        // The last step can't be undone from the phone, so it asks once.
        Alpine.store("modal").confirm({
          title: "Trip complete?",
          message: "This ends the trip on your link. Call dispatch if it was a mistake.",
          variant: "gold",
          confirmText: "Trip complete",
          cancelText: "Not yet",
          onConfirm: function () { return self.setStatus("done"); },
        });
      },

      goBack: function () {
        var self = this;
        if (!this.state.back || this.busy) return;
        Alpine.store("modal").confirm({
          title: "Go back one step?",
          message: "Use this if you tapped too soon. Dispatch sees the change.",
          variant: "info",
          confirmText: "Go back",
          onConfirm: function () { return self.setStatus(self.state.back); },
        });
      },

      toggleCircling: async function () {
        if (this.busy || !this.state.can_circle) return;
        this.busy = true;
        try {
          await this.send(cfg.urls.circling, { on: !this.state.circling });
        } finally {
          this.busy = false;
        }
      },

      reportNoShow: function () {
        var self = this;
        Alpine.store("modal").show({
          title: "Report a no-show",
          message: "Dispatch is told right away. Your trip status stays the same.",
          html:
            '<label for="dl-no-show-note" class="mt-3 block text-sm font-medium text-ink">Note (optional)</label>' +
            '<textarea id="dl-no-show-note" class="field mt-1.5" rows="3" maxlength="500"' +
            ' placeholder="Where you waited, and for how long"></textarea>',
          variant: "danger",
          confirmText: "Send report",
          onConfirm: function () {
            var box = document.getElementById("dl-no-show-note");
            var note = box ? box.value.trim() : "";
            return self.send(cfg.urls.no_show, { note: note }).then(function (ok) {
              if (!ok) {
                Alpine.store("toast").push({ type: "danger", title: self.notice || NO_SIGNAL });
                // Rejecting keeps the modal open with the note still typed.
                return Promise.reject(new Error("not sent"));
              }
              Alpine.store("toast").push({ type: "success", title: "Dispatch has your report" });
            });
          },
        });
      },

      /* ---- location ---------------------------------------------------- */
      syncWatch: function () {
        var want =
          this.state.share_location &&
          !this.state.done &&
          document.visibilityState === "visible" &&
          !this.locOff;
        if (want && geo.watchId === null) this.startWatch();
        else if (!want && geo.watchId !== null) this.stopWatch();
      },

      startWatch: function () {
        var self = this;
        if (!("geolocation" in navigator)) {
          this.locOff = true;
          return;
        }
        geo.watchId = navigator.geolocation.watchPosition(
          function (pos) { self.onFix(pos); },
          function (err) {
            if (err && err.code === 1) {
              // Permission denied: stop asking; status taps still go through.
              self.locOff = true;
              self.stopWatch();
            }
          },
          { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 }
        );
      },

      stopWatch: function () {
        if (geo.watchId !== null) navigator.geolocation.clearWatch(geo.watchId);
        geo.watchId = null;
        this.sharing = false;
        if (dot) {
          try { dot.remove(); } catch (e) { /* map gone */ }
          dot = null;
        }
      },

      onFix: function (pos) {
        var c = pos.coords;
        var fix = {
          lat: c.latitude,
          lng: c.longitude,
          accuracy: c.accuracy,
          speed: c.speed,
          heading: c.heading,
          at: pos.timestamp || Date.now(),
        };
        geo.last = fix;
        this.sharing = true;
        this.locOff = false;
        this.placeDot(fix);
        var now = Date.now();
        var due =
          !geo.sent ||
          now - geo.sentAt >= PING_EVERY_MS ||
          haversine(geo.sent, fix) >= PING_METERS;
        if (due) {
          geo.sent = fix;
          geo.sentAt = now;
          this.ping(fix);
        }
      },

      ping: function (fix) {
        var body = {
          lat: fix.lat,
          lng: fix.lng,
          recorded_at: new Date(fix.at).toISOString(),
        };
        if (finite(fix.accuracy)) body.accuracy = fix.accuracy;
        if (finite(fix.speed)) body.speed = fix.speed;
        if (finite(fix.heading)) body.heading = fix.heading;
        try {
          fetch(cfg.urls.ping, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": cfg.csrf },
            body: JSON.stringify(body),
            credentials: "same-origin",
            referrerPolicy: "same-origin",
            keepalive: true,
          }).catch(function () { /* fire-and-forget: no retry */ });
        } catch (e) { /* fire-and-forget */ }
      },

      /* ---- map --------------------------------------------------------- */
      startMap: function () {
        var self = this;
        var points = this.stops.map(stopPoint);
        if (!cfg.map_key || !points.length || points.indexOf(null) >= 0) return;
        function go() {
          if (!window.maplibregl) return;
          self.mapOn = true;
          self.$nextTick(function () { self.drawMap(points); });
        }
        // MapLibre loads deferred, after Alpine; wait for it if it isn't here yet.
        if (window.maplibregl || document.readyState === "complete") go();
        else window.addEventListener("load", go);
      },

      drawMap: function (points) {
        var self = this;
        try {
          var line = [];
          try {
            line = cfg.route
              ? decodePolyline(cfg.route).map(function (p) { return [p[1], p[0]]; })
              : [];
          } catch (e) {
            line = [];
          }
          // No cached route (or a broken one): straight segments between the stops.
          if (line.length < 2) line = points;
          var loaded = false;
          mapBounds = boundsOf(line.concat(points));
          map = new maplibregl.Map({
            container: this.$refs.map,
            style: STYLE_URL + encodeURIComponent(cfg.map_key),
            bounds: mapBounds,
            fitBoundsOptions: { padding: 40, maxZoom: 15 },
            attributionControl: { compact: true },
            cooperativeGestures: true,
          });
          map.on("error", function () {
            if (!loaded) self.mapFailed = true;
          });
          map.on("load", function () {
            loaded = true;
            fitMap();
            try {
              map.addSource("route", {
                type: "geojson",
                data: {
                  type: "Feature",
                  properties: {},
                  geometry: { type: "LineString", coordinates: line },
                },
              });
              map.addLayer({
                id: "route",
                type: "line",
                source: "route",
                layout: { "line-join": "round", "line-cap": "round" },
                paint: { "line-color": "#17191D", "line-width": 4 },
              });
            } catch (e) { /* pins still show the stops */ }
          });
          points.forEach(function (p, i) {
            new maplibregl.Marker({ element: stopPin(i + 1) }).setLngLat(p).addTo(map);
          });
          try {
            if (window.ResizeObserver) {
              new ResizeObserver(function () {
                try { map.resize(); } catch (e) { /* map gone */ }
              }).observe(this.$refs.map);
            }
          } catch (e) { /* no observer; load and tab show still refit */ }
          if (geo.last && geo.watchId !== null) this.placeDot(geo.last);
        } catch (e) {
          this.mapFailed = true;
          map = null;
        }
      },

      placeDot: function (fix) {
        if (!map) return;
        try {
          var at = [fix.lng, fix.lat];
          if (dot) dot.setLngLat(at);
          else dot = new maplibregl.Marker({ element: driverDot() }).setLngLat(at).addTo(map);
        } catch (e) { /* the map is decoration; never let it break the page */ }
      },
    };
  }

  window.driverPage = driverPage;
})();
