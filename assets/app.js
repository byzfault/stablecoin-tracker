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

  /* Fixed entity → palette slot.
   *
   * Colour follows the entity, never its rank, so filtering never repaints a
   * surviving series. The eight categorical slots are a validated palette:
   * every adjacent pair clears the colourblind and normal-vision separation
   * floors in both modes. "Other" is a residual bucket rather than a peer, so
   * it takes neutral grey — which is also why there is no ninth hue. */
  var CHAIN_SLOT = {
    Ethereum: '--series-1', Tron: '--series-2', Solana: '--series-3', BSC: '--series-4',
    Base: '--series-5', Arbitrum: '--series-6', Polygon: '--series-7', TON: '--series-8',
    Other: '--series-other'
  };
  var ISSUER_SLOT = {
    USDT: '--series-1', USDC: '--series-3', USDS: '--series-4', DAI: '--series-2',
    USDe: '--series-7', USD1: '--series-5', Other: '--series-other'
  };

  /* Networks offered in the filter. Deliberately short — these three are 84% of
   * supply, and a twenty-option dropdown is a worse control. The data behind it
   * still spans every tracked chain. */
  var FILTER_CHAINS = ['Ethereum', 'Tron', 'Solana'];

  var ALL = '__all__';

  var charts = [];

  /* Kept out of `charts` on purpose. draw() disposes that whole array and
   * rebuilds only the three supply charts, so a cost chart parked in there
   * would be destroyed by the first filter change and never come back. The
   * corridor panel does not respond to the filters at all — it owns its own
   * chart's lifecycle. */
  var costChart = null;
  var state = { rangeDays: 0, chain: ALL, issuer: ALL, data: null };

  // ---- helpers ----------------------------------------------------------

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function usd(value) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    var abs = Math.abs(value);
    if (abs >= 1e12) return '$' + (value / 1e12).toFixed(2) + 'T';
    if (abs >= 1e9) return '$' + (value / 1e9).toFixed(1) + 'B';
    if (abs >= 1e6) return '$' + (value / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return '$' + (value / 1e3).toFixed(1) + 'K';
    return '$' + value.toFixed(0);
  }

  function usdExact(value) { return '$' + Math.round(value).toLocaleString('en-US'); }

  function fmtDate(unixSeconds) {
    return new Date(unixSeconds * 1000).toLocaleDateString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric'
    });
  }

  function fmtMonth(iso) {
    return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', {
      month: 'short', year: 'numeric', timeZone: 'UTC'
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function sliceRange(bundle, days) {
    if (!days || days >= bundle.dates.length) return bundle;
    var from = bundle.dates.length - days;
    var out = { dates: bundle.dates.slice(from), series: {} };
    Object.keys(bundle.series).forEach(function (key) {
      out.series[key] = bundle.series[key].slice(from);
    });
    return out;
  }

  // ---- deriving series from the cube ------------------------------------

  /* The two filters are not independent views of the same numbers: answering
   * "USDC on Solana" needs the issuer x chain cross-section, which is what the
   * cube holds. Both groupings are derived from it so a filtered view can never
   * disagree with the headline total. */

  function groupByChain(issuerFilter) {
    var m = state.data.matrix;
    var series = {};
    m.chains.forEach(function (chain) {
      series[chain] = m.dates.map(function (_, i) {
        if (issuerFilter !== ALL) return m.cube[issuerFilter][chain][i];
        return m.issuers.reduce(function (sum, iss) { return sum + m.cube[iss][chain][i]; }, 0);
      });
    });
    return { dates: m.dates, series: series };
  }

  function groupByIssuer(chainFilter) {
    var m = state.data.matrix;
    var series = {};
    m.issuers.forEach(function (issuer) {
      series[issuer] = m.dates.map(function (_, i) {
        if (chainFilter !== ALL) return m.cube[issuer][chainFilter][i];
        return m.chains.reduce(function (sum, ch) { return sum + m.cube[issuer][ch][i]; }, 0);
      });
    });
    return { dates: m.dates, series: series };
  }

  /** Drop series that are flat zero across the window — an empty band in the
   *  legend is noise, and DAI on Tron is genuinely nothing. */
  function dropEmpty(bundle) {
    var out = { dates: bundle.dates, series: {} };
    Object.keys(bundle.series).forEach(function (key) {
      if (bundle.series[key].some(function (v) { return v > 0; })) out.series[key] = bundle.series[key];
    });
    return out;
  }

  // ---- charts ------------------------------------------------------------

  function baseOption(dates) {
    var muted = cssVar('--text-muted');
    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: muted, width: 1 } },
        backgroundColor: cssVar('--surface-1'),
        borderColor: cssVar('--border-strong'),
        borderWidth: 1,
        textStyle: { color: cssVar('--text-primary'), fontSize: 12 },
        extraCssText: 'box-shadow:0 4px 16px rgb(0 0 0 / 0.12); border-radius:8px;'
      },
      legend: {
        type: 'scroll', bottom: 0, icon: 'roundRect',
        itemWidth: 10, itemHeight: 10, itemGap: 14,
        textStyle: { color: cssVar('--text-secondary'), fontSize: 12 }
      },
      grid: { left: 8, right: 16, top: 16, bottom: 46, containLabel: true },
      xAxis: {
        type: 'category', boundaryGap: false,
        data: dates.map(fmtDate),
        axisLine: { lineStyle: { color: cssVar('--grid') } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11, hideOverlap: true },
        axisPointer: { label: { show: false } }
      },
      animationDuration: 350
    };
  }

  /* The 2px surface-coloured line is a spacer between stacked fills, not an
   * outline — it keeps adjacent bands legible without boxing the marks. */
  function buildSeries(bundle, slots, order, asPercent) {
    var surface = cssVar('--surface-1');
    var keys = order.filter(function (k) { return bundle.series[k]; });
    return keys.map(function (key) {
      var values = bundle.series[key];
      return {
        name: key,
        type: 'line',
        stack: 'total',
        areaStyle: { color: cssVar(slots[key] || '--series-other'), opacity: 1 },
        lineStyle: { width: 2, color: surface },
        showSymbol: false, symbolSize: 8,
        emphasis: { focus: 'series' },
        data: asPercent ? values.map(function (v, i) {
          var total = keys.reduce(function (s, k) { return s + bundle.series[k][i]; }, 0);
          return total ? +(v / total * 100).toFixed(2) : 0;
        }) : values
      };
    });
  }

  function renderChart(elId, bundle, slots, order, asPercent) {
    var el = document.getElementById(elId);
    var chart = echarts.init(el, null, { renderer: 'canvas' });
    var option = baseOption(bundle.dates);
    option.yAxis = {
      type: 'value',
      max: asPercent ? 100 : undefined,
      axisLabel: {
        color: cssVar('--text-muted'), fontSize: 11,
        formatter: asPercent ? function (v) { return v + '%'; } : usd
      },
      splitLine: { lineStyle: { color: cssVar('--grid'), type: 'solid' } }
    };
    option.series = buildSeries(bundle, slots, order, asPercent);
    option.tooltip.valueFormatter = asPercent
      ? function (v) { return v.toFixed(1) + '%'; }
      : function (v) { return usd(v); };
    chart.setOption(option, true);
    return chart;
  }

  // ---- table view (relief for low-contrast slots, and for screen readers)

  function renderTable(containerId, bundle, slots, order) {
    var el = document.getElementById(containerId);
    if (!el) return;
    var keys = order.filter(function (k) { return bundle.series[k]; });

    var rows = [];
    var count = Math.min(bundle.dates.length, 40);
    for (var i = bundle.dates.length - 1; i >= bundle.dates.length - count; i--) rows.push(i);

    var html = '<table><caption class="sr-only">Values behind the chart, most recent first</caption>'
             + '<thead><tr><th scope="col">Date</th>';
    keys.forEach(function (k) {
      html += '<th scope="col"><span class="swatch" style="background:'
            + cssVar(slots[k] || '--series-other') + '"></span>' + escapeHtml(k) + '</th>';
    });
    html += '</tr></thead><tbody>';
    rows.forEach(function (i) {
      html += '<tr><th scope="row">' + fmtDate(bundle.dates[i]) + '</th>';
      keys.forEach(function (k) { html += '<td>' + usdExact(bundle.series[k][i]) + '</td>'; });
      html += '</tr>';
    });
    el.innerHTML = html + '</tbody></table>';
  }

  // ---- headline ----------------------------------------------------------

  function renderHeadline() {
    var s = state.data.summary;
    var ref = state.data.reference;
    document.getElementById('asOf').textContent = fmtDate(s.as_of);

    var up = (s.change_30d_pct || 0) >= 0;

    /* Deliberately short. The share-of-money figures are not repeated here —
     * they are the panel immediately to the right, and saying them twice makes
     * both weaker while pushing the charts below the fold. */
    var rows = [
      ['Total supply', usd(s.total_supply), 'outstanding'],
      ['30-day change',
       '<span class="' + (up ? 'is-pos' : 'is-neg') + '">'
         + (up ? '+' : '') + (s.change_30d_pct === null ? '—' : s.change_30d_pct.toFixed(1) + '%')
         + '</span>',
       (up ? '+' : '−') + usd(Math.abs(s.change_30d || 0))],
      ['Top chain', escapeHtml(s.top_chain.name), s.top_chain.share_pct.toFixed(0) + '% of supply'],
      ['Top issuer', escapeHtml(s.top_issuer.name), s.top_issuer.share_pct.toFixed(0) + '% of supply'],
      ['Transfer volume', '<span class="pending">v2</span>', 'supply ≠ throughput']
    ];

    document.getElementById('statsBody').innerHTML = rows.map(function (r) {
      return '<tr><th scope="row">' + r[0] + '</th><td class="stat-value">' + r[1]
           + '</td><td class="stat-context">' + r[2] + '</td></tr>';
    }).join('');

    document.getElementById('statsNote').textContent = '';
  }

  /* Proportion bars rather than a pie: stablecoins are ~1% of M2, and a 1%
   * slice of a two-slice pie is a hairline nobody can read. A filled bar per
   * aggregate stays legible at any ratio. */
  function renderScale() {
    var s = state.data.summary;
    var ref = state.data.reference;
    var host = document.getElementById('scaleBars');
    if (!ref || !ref.aggregates || !ref.aggregates.length) {
      host.innerHTML = '<p class="section-note">Monetary aggregates unavailable in the last refresh.</p>';
      return;
    }

    host.innerHTML = ref.aggregates.map(function (a) {
      var pct = s.total_supply / a.value_usd * 100;
      return ''
        + '<div class="scale-row">'
        +   '<div class="scale-head">'
        +     '<span class="scale-label">' + escapeHtml(a.label) + '</span>'
        +     '<span class="scale-pct">' + pct.toFixed(1) + '%</span>'
        +   '</div>'
        +   '<div class="scale-track" role="img" aria-label="Stablecoins are '
        +      pct.toFixed(1) + ' percent of ' + escapeHtml(a.label) + '">'
        +     '<span class="scale-fill" style="width:' + Math.max(pct, 0.4).toFixed(2) + '%"></span>'
        +   '</div>'
        +   '<p class="scale-meta">' + usd(a.value_usd) + ' total · ' + fmtMonth(a.as_of)
        +     ' · <span class="scale-note">' + escapeHtml(a.note) + '</span></p>'
        + '</div>';
    }).join('');

    document.getElementById('scaleNote').innerHTML =
      'Stablecoin supply ' + usd(s.total_supply) + ' against aggregates from '
      + '<a href="' + ref.source_url + '">FRED</a>. Series publish on different schedules, '
      + 'so each carries its own date.';
  }

  function renderStatus(meta) {
    var strip = document.getElementById('statusStrip');
    if (!meta) return;
    var ageDays = (Date.now() - Date.parse(meta.generated_at)) / 86400000;
    var messages = [];

    if (meta.status !== 'ok' && meta.errors && meta.errors.length) {
      messages.push('The last refresh was incomplete (' + meta.errors.length + ' endpoint error'
        + (meta.errors.length === 1 ? '' : 's') + '); some figures may be from an earlier snapshot.');
    }
    if (ageDays > 2) messages.push('Last successful refresh was ' + Math.floor(ageDays) + ' days ago.');
    // data_notes are deliberately NOT surfaced here. They record upstream quirks
    // the pipeline handled — a single clamped balance from March 2025 is an audit
    // detail for meta.json and METHODOLOGY.md, not a banner above the charts.

    if (messages.length) { strip.textContent = messages.join(' '); strip.hidden = false; }
  }

  // ---- corridor signals (proxy) -----------------------------------------

  /* Every panel that renders a snapshot prints where the number came from and
   * how often it refreshes. A dashboard that shows a figure without a date is
   * asking to be believed on trust it has not earned. */
  function fmtStamp(iso) {
    if (!iso) return 'unknown';
    var d = new Date(iso);
    if (isNaN(d)) return 'unknown';
    return d.toLocaleDateString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'
    }) + ', ' + d.toLocaleTimeString('en-GB', {
      hour: '2-digit', minute: '2-digit', timeZone: 'UTC'
    }) + ' UTC';
  }

  function hoursSince(iso) {
    var t = Date.parse(iso);
    return isNaN(t) ? null : (Date.now() - t) / 3600000;
  }

  /* Stale is a property of the data, not of the fetch: a run that succeeds at
   * 06:00 and serves a snapshot from three days ago is stale, and saying
   * "updated this morning" about it would be the lie the badge exists to
   * prevent. */
  function renderPanelMeta(elId, opts) {
    var el = document.getElementById(elId);
    if (!el) return;
    var age = hoursSince(opts.asOf);
    var stale = opts.stale || (age !== null && age > opts.staleAfterHours);
    var parts = ['Data as of <time datetime="' + escapeHtml(opts.asOf || '') + '">'
                 + fmtStamp(opts.asOf) + '</time>'];
    parts.push(opts.sourceUrl
      ? '<a href="' + escapeHtml(opts.sourceUrl) + '">' + escapeHtml(opts.source) + '</a>'
      : escapeHtml(opts.source));
    parts.push('updated ' + escapeHtml(opts.cadence));
    el.innerHTML = parts.join(' · ')
      + (stale ? ' <span class="stale-badge" title="This panel is older than its refresh '
                 + 'cadence. The last good snapshot is being shown.">stale</span>' : '');
  }

  function trendCell(pct) {
    if (pct === null || pct === undefined) return '<td class="num muted">—</td>';
    var cls = pct >= 0 ? 'is-pos' : 'is-neg';
    return '<td class="num ' + cls + '">' + (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%</td>';
  }

  /* Heat by share of the largest corridor, on a square root so the long tail
   * stays visible. The value is always printed as text — colour is the second
   * encoding, never the only one. */
  function heatStyle(value, max) {
    if (!max) return '';
    var t = Math.sqrt(Math.max(value, 0) / max);
    return ' style="background:color-mix(in srgb, var(--accent) '
         + (t * 26).toFixed(1) + '%, transparent)"';
  }

  function renderCorridors(data) {
    var body = document.getElementById('corridorBody');
    var costSection = document.querySelector('.corridor-cost');

    if (!data || !data.corridors || !data.corridors.length) {
      body.innerHTML = '<p class="section-note">'
        + 'No corridor data yet. Set <code>corridor.dune_query_id</code> in '
        + '<code>config.json</code> and run <code>scripts/fetch_dune.py</code> with '
        + '<code>DUNE_API_KEY</code> set, then <code>scripts/build_corridors.py</code>.'
        + '</p>';
      if (costSection) costSection.hidden = true;
      document.getElementById('corridorMeta').textContent = '';
      return;
    }

    renderPanelMeta('corridorMeta', {
      asOf: data.data_as_of || data.fetched_at,
      source: data.source_name || 'Dune Analytics',
      sourceUrl: data.source_url,
      cadence: data.update_cadence || 'daily',
      stale: data.stale,
      staleAfterHours: 48
    });

    var attribution = data.attribution || {};
    var top = data.corridors.slice(0, 12);
    var max = top.length ? top[0].proxy_volume_usd : 0;

    var html = ''
      + '<p class="attribution-line">'
      +   '<strong>' + (attribution.corridor_share_pct || 0).toFixed(1) + '%</strong> of labelled '
      +   'exchange-to-exchange volume in this window could be attributed to a market pair. '
      +   '<strong>' + (attribution.unattributed_share_pct || 0).toFixed(1) + '%</strong> touched a '
      +   'global venue or an unmapped one and is deliberately left out of every corridor below.'
      + '</p>'
      + '<div class="table-wrap is-open"><table class="corridor-table">'
      + '<caption class="sr-only">Top market pairs by proxy volume over the last '
      +   (data.window ? data.window.days : 90) + ' days</caption>'
      + '<thead><tr>'
      +   '<th scope="col">Corridor</th>'
      +   '<th scope="col" class="num">Proxy volume</th>'
      +   '<th scope="col" class="num">Transfers</th>'
      +   '<th scope="col" class="num">30d trend</th>'
      +   '<th scope="col" class="num">Traditional, annual</th>'
      +   '<th scope="col">Mapping</th>'
      + '</tr></thead><tbody>';

    top.forEach(function (c) {
      var traditional = c.traditional_annual_usd
        ? usd(c.traditional_annual_usd) + ' <span class="cell-note">bilateral</span>'
        : (c.traditional_received_total_usd
            ? usd(c.traditional_received_total_usd)
              + ' <span class="cell-note">all sources, ' + (c.traditional_received_year || '') + '</span>'
            : '—');
      html += '<tr>'
        + '<th scope="row">' + escapeHtml(c.from_name) + ' <span class="arrow">→</span> '
        +   escapeHtml(c.to_name) + '</th>'
        + '<td class="num"' + heatStyle(c.proxy_volume_usd, max) + '>' + usd(c.proxy_volume_usd) + '</td>'
        + '<td class="num">' + c.transfer_count.toLocaleString('en-US') + '</td>'
        + trendCell(c.trend_30d_pct)
        + '<td class="num">' + traditional + '</td>'
        + '<td><span class="confidence is-' + escapeHtml(c.confidence) + '">'
        +   escapeHtml(c.confidence) + '</span></td>'
        + '</tr>';
    });

    body.innerHTML = html + '</tbody></table></div>';

    renderCorridorDetail(data);
    if (costSection) costSection.hidden = false;
    renderCostChart();
  }

  /* The detail view exists so a corridor can be audited back to the venue pairs
   * it was built from. A single mislabelled address is the most likely way this
   * module goes wrong, and hiding the venues would make that invisible. */
  function renderCorridorDetail(data) {
    var el = document.getElementById('corridorDetail');
    if (!el) return;
    var html = '<table><caption class="sr-only">Venue pairs behind each corridor</caption>'
      + '<thead><tr><th scope="col">Corridor</th><th scope="col">Venue pairs</th>'
      + '<th scope="col" class="num">USDT</th><th scope="col" class="num">USDC</th>'
      + '<th scope="col" class="num">Remittance cost</th></tr></thead><tbody>';

    data.corridors.slice(0, 12).forEach(function (c) {
      var pairs = (c.venue_pairs || []).slice(0, 4).map(function (p) {
        return escapeHtml(p.pair) + ' (' + usd(p.usd_volume) + ')';
      }).join('<br>') || '—';
      var cost = c.remittance_cost_pct === null || c.remittance_cost_pct === undefined
        ? '—'
        : c.remittance_cost_pct.toFixed(2) + '% <span class="cell-note">'
          + (c.remittance_cost_source === 'corridor'
              ? 'corridor, ' + escapeHtml(c.remittance_cost_period || '')
              : 'country average, ' + (c.remittance_cost_year || '')) + '</span>';
      html += '<tr><th scope="row">' + escapeHtml(c.from_market) + ' → ' + escapeHtml(c.to_market)
        + '</th><td class="pairs">' + pairs + '</td>'
        + '<td class="num">' + usd((c.tokens || {}).USDT || 0) + '</td>'
        + '<td class="num">' + usd((c.tokens || {}).USDC || 0) + '</td>'
        + '<td class="num">' + cost + '</td></tr>';
    });
    el.innerHTML = html + '</tbody></table>';
  }

  /* Two bars per corridor rather than a ratio: a "12x cheaper" headline would
   * be the most quotable thing on the page and the least defensible, since the
   * on-chain side is an assumed fee and the traditional side is a measured
   * all-in price including cash handling and FX. */
  function renderCostChart() {
    var el = document.getElementById('chartCost');
    var data = state.data && state.data.corridors;
    if (!el || !data || !data.corridors) return;
    var withCost = data.corridors.slice(0, 12).filter(function (c) {
      return c.remittance_cost_pct !== null && c.remittance_cost_pct !== undefined;
    }).slice(0, 8);

    var note = document.getElementById('corridorCostNote');
    if (!withCost.length) {
      el.innerHTML = '';
      if (note) note.textContent = 'No remittance cost figures published for these destinations.';
      return;
    }

    var labels = withCost.map(function (c) { return c.from_market + ' → ' + c.to_market; });
    // Colours are read from CSS variables at build time, so a theme change
    // means rebuilding rather than restyling.
    if (costChart) costChart.dispose();
    costChart = echarts.init(el, null, { renderer: 'canvas' });
    var muted = cssVar('--text-muted');

    costChart.setOption({
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: cssVar('--surface-1'),
        borderColor: cssVar('--border-strong'),
        borderWidth: 1,
        textStyle: { color: cssVar('--text-primary'), fontSize: 12 },
        valueFormatter: function (v) { return v.toFixed(2) + '%'; },
        extraCssText: 'box-shadow:0 4px 16px rgb(0 0 0 / 0.12); border-radius:8px;'
      },
      legend: {
        bottom: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10, itemGap: 14,
        textStyle: { color: cssVar('--text-secondary'), fontSize: 12 }
      },
      grid: { left: 8, right: 24, top: 8, bottom: 40, containLabel: true },
      xAxis: {
        type: 'value',
        axisLabel: { color: muted, fontSize: 11, formatter: function (v) { return v + '%'; } },
        splitLine: { lineStyle: { color: cssVar('--grid') } }
      },
      yAxis: {
        type: 'category',
        data: labels.slice().reverse(),
        axisLine: { lineStyle: { color: cssVar('--grid') } },
        axisTick: { show: false },
        axisLabel: { color: cssVar('--text-secondary'), fontSize: 11 }
      },
      series: [
        {
          name: 'Traditional remittance',
          type: 'bar',
          itemStyle: { color: cssVar('--series-2') },
          barMaxWidth: 14,
          data: withCost.map(function (c) { return +c.remittance_cost_pct.toFixed(2); }).reverse()
        },
        {
          name: 'On-chain (assumed fees)',
          type: 'bar',
          itemStyle: { color: cssVar('--series-1') },
          barMaxWidth: 14,
          data: withCost.map(function (c) { return c.onchain_cost_pct || 0; }).reverse()
        }
      ],
      animationDuration: 350
    }, true);

    if (note) {
      note.textContent = (data.cost_assumptions || {}).note || '';
    }
  }

  // ---- filters and drawing ----------------------------------------------

  function buildFilters() {
    var m = state.data.matrix;

    var chainSelect = document.getElementById('chainFilter');
    chainSelect.innerHTML = '<option value="' + ALL + '">All networks</option>'
      + FILTER_CHAINS.map(function (c) { return '<option value="' + c + '">' + c + '</option>'; }).join('')
      + '<option value="Other">Other networks</option>';

    // Ordered by current supply so the list opens with what matters.
    var last = m.dates.length - 1;
    var issuers = m.issuers.filter(function (i) { return i !== 'Other'; }).sort(function (a, b) {
      var sa = m.chains.reduce(function (s, c) { return s + m.cube[a][c][last]; }, 0);
      var sb = m.chains.reduce(function (s, c) { return s + m.cube[b][c][last]; }, 0);
      return sb - sa;
    });
    var issuerSelect = document.getElementById('issuerFilter');
    issuerSelect.innerHTML = '<option value="' + ALL + '">All stablecoins</option>'
      + issuers.map(function (i) { return '<option value="' + i + '">' + i + '</option>'; }).join('')
      + '<option value="Other">Other stablecoins</option>';
  }

  function describeFilter() {
    var chain = state.chain === ALL ? 'all networks' : state.chain;
    var issuer = state.issuer === ALL ? 'all stablecoins' : state.issuer;
    var m = state.data.matrix;
    var last = m.dates.length - 1;

    var value;
    if (state.chain === ALL && state.issuer === ALL) value = m.total[last];
    else if (state.chain === ALL) value = m.chains.reduce(function (s, c) { return s + m.cube[state.issuer][c][last]; }, 0);
    else if (state.issuer === ALL) value = m.issuers.reduce(function (s, i) { return s + m.cube[i][state.chain][last]; }, 0);
    else value = m.cube[state.issuer][state.chain][last];

    document.getElementById('filterSummary').textContent =
      'Showing ' + issuer + ' on ' + chain + ' — ' + usd(value) + ' outstanding';
  }

  function draw() {
    charts.forEach(function (c) { c.dispose(); });
    charts = [];

    var byChain = dropEmpty(sliceRange(groupByChain(state.issuer), state.rangeDays));
    var byIssuer = dropEmpty(sliceRange(groupByIssuer(state.chain), state.rangeDays));
    var chainOrder = state.data.matrix.chains;
    var issuerOrder = state.data.matrix.issuers;

    // Titles state what is actually on screen, so a filtered chart is never
    // mistaken for the whole market.
    var issuerLabel = state.issuer === ALL ? 'All stablecoins' : state.issuer;
    var chainLabel = state.chain === ALL ? 'all networks' : state.chain;

    document.getElementById('chartChainsSub').textContent =
      issuerLabel + ' — circulating supply, stacked by chain.';
    document.getElementById('chartShareSub').textContent =
      issuerLabel + ' — share of its supply held on each chain.';
    document.getElementById('chartIssuersSub').textContent =
      'Supply by issuer on ' + chainLabel + '. “Other” is every stablecoin outside the six largest.';

    charts.push(renderChart('chartChains', byChain, CHAIN_SLOT, chainOrder, false));
    charts.push(renderChart('chartShare', byChain, CHAIN_SLOT, chainOrder, true));
    charts.push(renderChart('chartIssuers', byIssuer, ISSUER_SLOT, issuerOrder, false));

    renderTable('tableChains', byChain, CHAIN_SLOT, chainOrder);
    renderTable('tableShare', byChain, CHAIN_SLOT, chainOrder);
    renderTable('tableIssuers', byIssuer, ISSUER_SLOT, issuerOrder);

    describeFilter();
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

    document.getElementById('chainFilter').addEventListener('change', function () {
      state.chain = this.value;
      this.classList.toggle('is-filtered', this.value !== ALL);
      draw();
    });
    document.getElementById('issuerFilter').addEventListener('change', function () {
      state.issuer = this.value;
      this.classList.toggle('is-filtered', this.value !== ALL);
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

    // Optional: present only if the markup includes a theme control. Colours are
    // read from CSS variables when a chart is built, so switching theme means
    // rebuilding the charts rather than restyling them.
    var themeToggle = document.getElementById('themeToggle');
    if (themeToggle) {
      themeToggle.addEventListener('click', function () {
        var isDark = document.documentElement.getAttribute('data-theme') === 'dark'
          || (!document.documentElement.hasAttribute('data-theme')
              && window.matchMedia('(prefers-color-scheme: dark)').matches);
        document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
        renderScale();
        renderCostChart();
        draw();
      });
    }

    // With no manual control, follow the OS setting live.
    var media = window.matchMedia('(prefers-color-scheme: dark)');
    var onSchemeChange = function () {
      if (!document.documentElement.hasAttribute('data-theme')) {
        renderScale();
        renderCostChart();
        draw();
      }
    };
    if (media.addEventListener) media.addEventListener('change', onSchemeChange);

    var resizeTimer;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        charts.forEach(function (c) { c.resize(); });
        if (costChart) costChart.resize();
      }, 120);
    });
  }

  async function init() {
    try {
      var files = ['summary', 'matrix', 'reference', 'meta'];
      var loaded = await Promise.all(files.map(function (name) {
        return fetch('data/' + name + '.json').then(function (r) {
          if (!r.ok) throw new Error(name + '.json returned HTTP ' + r.status);
          return r.json();
        });
      }));

      /* Corridors load separately and are allowed to be absent. The module is
       * optional — it needs a Dune key the repo cannot carry — and a dashboard
       * that refuses to render its supply charts because an optional proxy panel
       * has no data would be trading a working page for a missing one. */
      var corridors = await fetch('data/corridors.json')
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; });

      state.data = {
        summary: loaded[0], matrix: loaded[1], reference: loaded[2], meta: loaded[3],
        corridors: corridors
      };

      renderHeadline();
      renderScale();
      renderStatus(state.data.meta);
      renderCorridors(corridors);
      buildFilters();
      bindControls();
      draw();
    } catch (error) {
      var el = document.getElementById('loadError');
      el.textContent = 'Could not load the data files. If you are running locally, serve the '
        + 'folder over HTTP (python3 -m http.server) rather than opening index.html directly — '
        + 'browsers block fetch() on file:// URLs. Details: ' + error.message;
      el.hidden = false;
    }
  }

  init();
})();
