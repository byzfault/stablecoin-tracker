/* Stablecoin Flows — dashboard.
 *
 * Reads only the JSON committed under data/. There are no API calls from the
 * browser: the pipeline resolves everything ahead of time, so the page cannot
 * break because an upstream service is slow, rate limited or down.
 *
 * Paths are relative on purpose. GitHub Pages serves a project site from
 * /<repo>/, so a leading slash would resolve to the user site root and 404.
 */
(function () {
  'use strict';

  /** Fixed chain → palette slot. Colour follows the entity, never its rank, so
   *  changing the time range never repaints a series. "Other" is a residual
   *  bucket rather than a peer, so it takes a neutral grey. */
  var SERIES_ORDER = [
    { key: 'Ethereum', slot: '--series-1' },
    { key: 'Tron', slot: '--series-2' },
    { key: 'Solana', slot: '--series-3' },
    { key: 'BSC', slot: '--series-4' },
    { key: 'Base', slot: '--series-5' },
    { key: 'Arbitrum', slot: '--series-6' },
    { key: 'Polygon', slot: '--series-7' },
    { key: 'TON', slot: '--series-8' },
    { key: 'Other', slot: '--series-other' }
  ];

  var ISSUER_ORDER = [
    { key: 'USDT', slot: '--series-1' },
    { key: 'USDC', slot: '--series-3' },
    { key: 'Other', slot: '--series-other' }
  ];

  var charts = [];
  var state = { rangeDays: 0, data: null };

  // ---- helpers ----------------------------------------------------------

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** Compact USD, e.g. $307.1B. Two significant places is as precise as a
   *  supply figure deserves; the table view carries the exact numbers. */
  function usd(value) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    var abs = Math.abs(value);
    if (abs >= 1e12) return '$' + (value / 1e12).toFixed(2) + 'T';
    if (abs >= 1e9) return '$' + (value / 1e9).toFixed(1) + 'B';
    if (abs >= 1e6) return '$' + (value / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return '$' + (value / 1e3).toFixed(1) + 'K';
    return '$' + value.toFixed(0);
  }

  function usdExact(value) {
    return '$' + Math.round(value).toLocaleString('en-US');
  }

  function fmtDate(unixSeconds) {
    return new Date(unixSeconds * 1000).toLocaleDateString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric'
    });
  }

  /** Trim a {dates, series} bundle to the last N days. 0 means everything. */
  function sliceRange(bundle, days) {
    if (!days || days >= bundle.dates.length) return bundle;
    var from = bundle.dates.length - days;
    var out = { dates: bundle.dates.slice(from), series: {} };
    Object.keys(bundle.series).forEach(function (key) {
      out.series[key] = bundle.series[key].slice(from);
    });
    if (bundle.total) out.total = bundle.total.slice(from);
    return out;
  }

  // ---- chart construction ----------------------------------------------

  function baseOption(dates) {
    var text = cssVar('--text-secondary');
    var muted = cssVar('--text-muted');
    var grid = cssVar('--grid');

    return {
      // A crosshair tooltip is the default for time series, not an extra.
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: muted, width: 1 } },
        backgroundColor: cssVar('--surface-1'),
        borderColor: cssVar('--border-strong'),
        borderWidth: 1,
        textStyle: { color: cssVar('--text-primary'), fontSize: 12 },
        extraCssText: 'box-shadow:0 4px 16px rgb(0 0 0 / 0.12); border-radius:8px;'
      },
      // Identity is never colour-alone: the legend is always present.
      legend: {
        type: 'scroll',
        bottom: 0,
        icon: 'roundRect',
        itemWidth: 10,
        itemHeight: 10,
        itemGap: 14,
        textStyle: { color: text, fontSize: 12 }
      },
      grid: { left: 8, right: 16, top: 16, bottom: 46, containLabel: true },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: dates.map(function (d) { return fmtDate(d); }),
        axisLine: { lineStyle: { color: grid } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11, hideOverlap: true },
        axisPointer: { label: { show: false } }
      },
      animationDuration: 400
    };
  }

  /** Stacked area. The 2px surface-coloured border is a spacer between
   *  segments, not an outline: it keeps adjacent fills legible without
   *  drawing a box around the marks. */
  function stackSeries(order, bundle, opts) {
    var surface = cssVar('--surface-1');
    return order
      .filter(function (s) { return bundle.series[s.key]; })
      .map(function (s) {
        return {
          name: s.key,
          type: 'line',
          stack: 'total',
          areaStyle: { color: cssVar(s.slot), opacity: 1 },
          lineStyle: { width: 2, color: surface },
          showSymbol: false,
          symbol: 'circle',
          symbolSize: 8,
          emphasis: { focus: 'series' },
          data: opts && opts.percent
            ? bundle.series[s.key].map(function (v, i) {
                var total = 0;
                order.forEach(function (o) {
                  if (bundle.series[o.key]) total += bundle.series[o.key][i];
                });
                return total ? +(v / total * 100).toFixed(2) : 0;
              })
            : bundle.series[s.key]
        };
      });
  }

  function renderStacked(el, bundle, order) {
    var chart = echarts.init(el, null, { renderer: 'canvas' });
    chart.setOption(Object.assign(baseOption(bundle.dates), {
      yAxis: {
        type: 'value',
        axisLabel: { color: cssVar('--text-muted'), fontSize: 11, formatter: usd },
        splitLine: { lineStyle: { color: cssVar('--grid'), type: 'solid' } }
      },
      series: stackSeries(order, bundle),
      tooltip: Object.assign(baseOption(bundle.dates).tooltip, {
        valueFormatter: function (v) { return usd(v); }
      })
    }), true);
    return chart;
  }

  function renderPercent(el, bundle, order) {
    var chart = echarts.init(el, null, { renderer: 'canvas' });
    chart.setOption(Object.assign(baseOption(bundle.dates), {
      yAxis: {
        type: 'value',
        max: 100,
        axisLabel: {
          color: cssVar('--text-muted'), fontSize: 11,
          formatter: function (v) { return v + '%'; }
        },
        splitLine: { lineStyle: { color: cssVar('--grid'), type: 'solid' } }
      },
      series: stackSeries(order, bundle, { percent: true }),
      tooltip: Object.assign(baseOption(bundle.dates).tooltip, {
        valueFormatter: function (v) { return v.toFixed(1) + '%'; }
      })
    }), true);
    return chart;
  }

  // ---- table view (the relief for low-contrast slots, and for screen readers)

  function renderTable(containerId, bundle, order) {
    var el = document.getElementById(containerId);
    if (!el) return;

    // Most recent first, capped: this is a readable companion to the chart,
    // not a data dump of 3,000 rows.
    var rows = [];
    var count = Math.min(bundle.dates.length, 40);
    for (var i = bundle.dates.length - 1; i >= bundle.dates.length - count; i--) rows.push(i);

    var keys = order.filter(function (s) { return bundle.series[s.key]; });

    var html = '<table><caption class="sr-only">Values behind the chart, most recent first</caption><thead><tr><th scope="col">Date</th>';
    keys.forEach(function (s) {
      html += '<th scope="col"><span class="swatch" style="background:' + cssVar(s.slot) + '"></span>' + s.key + '</th>';
    });
    html += '</tr></thead><tbody>';

    rows.forEach(function (i) {
      html += '<tr><th scope="row">' + fmtDate(bundle.dates[i]) + '</th>';
      keys.forEach(function (s) {
        html += '<td>' + usdExact(bundle.series[s.key][i]) + '</td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  }

  // ---- KPIs -------------------------------------------------------------

  function renderKpis(summary) {
    document.getElementById('kpiTotal').textContent = usd(summary.total_supply);
    document.getElementById('asOf').textContent = fmtDate(summary.as_of);

    var changeEl = document.getElementById('kpiChange');
    if (summary.change_30d_pct === null || summary.change_30d_pct === undefined) {
      changeEl.textContent = '—';
    } else {
      var up = summary.change_30d_pct >= 0;
      changeEl.textContent = (up ? '+' : '') + summary.change_30d_pct.toFixed(2) + '%';
      changeEl.className = 'kpi-value ' + (up ? 'is-pos' : 'is-neg');
      document.getElementById('kpiChangeNote').textContent =
        (up ? '+' : '−') + usd(Math.abs(summary.change_30d)).replace('$', '$') + ' over 30 days';
    }

    document.getElementById('kpiChain').textContent = summary.top_chain.name;
    document.getElementById('kpiChainNote').textContent =
      usd(summary.top_chain.supply) + ' · ' + summary.top_chain.share_pct.toFixed(1) + '% of supply';

    document.getElementById('kpiIssuer').textContent = summary.top_issuer.name;
    document.getElementById('kpiIssuerNote').textContent =
      usd(summary.top_issuer.supply) + ' · ' + summary.top_issuer.share_pct.toFixed(1) + '% of supply';
  }

  /** Staleness and partial refreshes are stated, never hidden. */
  function renderStatus(meta) {
    var strip = document.getElementById('statusStrip');
    if (!meta) return;

    var ageDays = (Date.now() - Date.parse(meta.generated_at)) / 86400000;
    var messages = [];

    if (meta.status !== 'ok' && meta.errors && meta.errors.length) {
      messages.push(
        'The last refresh was incomplete (' + meta.errors.length +
        ' endpoint error' + (meta.errors.length === 1 ? '' : 's') +
        '); some figures may be from an earlier snapshot.'
      );
    }
    if (ageDays > 2) {
      messages.push('Last successful refresh was ' + Math.floor(ageDays) + ' days ago.');
    }

    if (messages.length) {
      strip.textContent = messages.join(' ');
      strip.hidden = false;
    }
  }

  // ---- wiring -----------------------------------------------------------

  function draw() {
    charts.forEach(function (c) { c.dispose(); });
    charts = [];

    var chains = sliceRange(state.data.chains, state.rangeDays);
    var issuers = sliceRange(state.data.issuers, state.rangeDays);

    charts.push(renderStacked(document.getElementById('chartChains'), chains, SERIES_ORDER));
    charts.push(renderPercent(document.getElementById('chartShare'), chains, SERIES_ORDER));
    charts.push(renderStacked(document.getElementById('chartIssuers'), issuers, ISSUER_ORDER));

    renderTable('tableChains', chains, SERIES_ORDER);
    renderTable('tableShare', chains, SERIES_ORDER);
    renderTable('tableIssuers', issuers, ISSUER_ORDER);
  }

  function bindControls() {
    document.getElementById('rangeControls').addEventListener('click', function (event) {
      var button = event.target.closest('button');
      if (!button) return;
      this.querySelectorAll('button').forEach(function (b) { b.classList.remove('is-active'); });
      button.classList.add('is-active');
      state.rangeDays = Number(button.dataset.days);
      draw();
    });

    document.querySelectorAll('.table-toggle').forEach(function (button) {
      button.addEventListener('click', function () {
        var target = document.getElementById(button.dataset.table);
        var open = target.hidden;
        target.hidden = !open;
        button.setAttribute('aria-expanded', String(open));
        button.textContent = open ? 'Hide table' : 'Table';
      });
    });

    var toggle = document.getElementById('themeToggle');
    toggle.addEventListener('click', function () {
      var isDark = document.documentElement.getAttribute('data-theme') === 'dark'
        || (!document.documentElement.hasAttribute('data-theme')
            && window.matchMedia('(prefers-color-scheme: dark)').matches);
      document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
      // Colours are read from CSS variables at build time, so a theme change
      // means rebuilding the charts rather than restyling them.
      draw();
    });

    var resizeTimer;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        charts.forEach(function (c) { c.resize(); });
      }, 120);
    });
  }

  function fail(message) {
    var el = document.getElementById('loadError');
    el.textContent = message;
    el.hidden = false;
  }

  async function init() {
    try {
      var results = await Promise.all([
        fetch('data/summary.json').then(function (r) { return r.json(); }),
        fetch('data/chains.json').then(function (r) { return r.json(); }),
        fetch('data/issuers.json').then(function (r) { return r.json(); }),
        fetch('data/meta.json').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
      ]);

      state.data = { summary: results[0], chains: results[1], issuers: results[2], meta: results[3] };

      renderKpis(state.data.summary);
      renderStatus(state.data.meta);
      bindControls();
      draw();
    } catch (error) {
      fail('Could not load the data files. If you are running locally, serve the folder over HTTP '
         + '(python3 -m http.server) rather than opening index.html directly — browsers block '
         + 'fetch() on file:// URLs. Details: ' + error.message);
    }
  }

  init();
})();
