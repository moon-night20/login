const $ = (id) => document.getElementById(id);
const svgNamespace = 'http://www.w3.org/2000/svg';

function formatNumber(value, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : '--';
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value || '--') : date.toLocaleString();
}

function setUnavailable(id, message) {
  $(id).textContent = message;
}

async function getJson(url) {
  const response = await fetch(url, {headers: {Accept: 'application/json'}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

function renderChart(id, records, key, digits) {
  const container = $(id);
  container.replaceChildren();
  const values = records.map((record) => Number(record[key])).filter(Number.isFinite);
  if (!values.length) {
    const empty = document.createElement('div');
    empty.className = 'chart-empty';
    empty.textContent = 'No telemetry records yet.';
    container.append(empty);
    return;
  }

  const width = 600;
  const height = 135;
  const padding = {top: 10, right: 44, bottom: 18, left: 8};
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || Math.max(Math.abs(maximum) * 0.1, 1);
  const points = values.map((value, index) => {
    const x = padding.left + (values.length === 1 ? plotWidth / 2 : index * plotWidth / (values.length - 1));
    const y = padding.top + plotHeight - (value - minimum) / range * plotHeight;
    return `${x},${y}`;
  });

  const svg = document.createElementNS(svgNamespace, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const gridline = document.createElementNS(svgNamespace, 'line');
  gridline.setAttribute('x1', padding.left);
  gridline.setAttribute('x2', width - padding.right);
  gridline.setAttribute('y1', padding.top + plotHeight);
  gridline.setAttribute('y2', padding.top + plotHeight);
  gridline.setAttribute('class', 'chart-gridline');
  svg.append(gridline);

  const polyline = document.createElementNS(svgNamespace, 'polyline');
  polyline.setAttribute('points', points.join(' '));
  polyline.setAttribute('class', 'chart-line');
  svg.append(polyline);

  const labels = [
    {text: formatNumber(maximum, digits), y: padding.top + 4},
    {text: formatNumber(minimum, digits), y: padding.top + plotHeight},
  ];
  labels.forEach(({text, y}) => {
    const label = document.createElementNS(svgNamespace, 'text');
    label.setAttribute('x', width - 2);
    label.setAttribute('y', y);
    label.setAttribute('text-anchor', 'end');
    label.setAttribute('class', 'chart-label');
    label.textContent = text;
    svg.append(label);
  });
  container.append(svg);
}

function renderTelemetry(records) {
  $('record-count').textContent = `${records.length} RECORD${records.length === 1 ? '' : 'S'}`;
  $('table-count').textContent = `${records.length} RECORD${records.length === 1 ? '' : 'S'}`;
  if (!records.length) {
    ['latest-rpm', 'target-rpm', 'latest-temperature', 'latest-pressure', 'latest-power', 'telemetry-time']
      .forEach((id) => setUnavailable(id, id === 'telemetry-time' ? 'WAITING FOR TELEMETRY' : '--'));
    $('telemetry-rows').innerHTML = '<tr><td colspan="6" class="empty-cell">Waiting for telemetry records.</td></tr>';
  } else {
    const latest = records[records.length - 1];
    $('latest-rpm').textContent = formatNumber(latest.rpm, 0);
    $('target-rpm').textContent = formatNumber(latest.target_rpm, 0);
    $('latest-temperature').textContent = formatNumber(latest.temperature, 2);
    $('latest-pressure').textContent = formatNumber(latest.pressure, 2);
    $('latest-power').textContent = formatNumber(latest.power, 2);
    $('telemetry-time').textContent = `LATEST ${formatTime(latest.timestamp)}`;

    const body = $('telemetry-rows');
    body.replaceChildren();
    records.slice().reverse().slice(0, 30).forEach((record) => {
      const row = document.createElement('tr');
      [formatTime(record.timestamp), record.motor_status || '--', formatNumber(record.rpm, 0),
        formatNumber(record.temperature, 2), formatNumber(record.pressure, 2), formatNumber(record.power, 2)]
        .forEach((value) => {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.append(cell);
        });
      body.append(row);
    });
  }

  renderChart('chart-rpm', records, 'rpm', 0);
  renderChart('chart-temperature', records, 'temperature', 2);
  renderChart('chart-pressure', records, 'pressure', 2);
  renderChart('chart-power', records, 'power', 2);
}

function renderEvents(events) {
  $('event-count').textContent = `${events.length} EVENT${events.length === 1 ? '' : 'S'}`;
  const list = $('event-list');
  list.replaceChildren();
  if (!events.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-cell';
    empty.textContent = 'No events recorded.';
    list.append(empty);
    return;
  }

  events.forEach((event) => {
    const row = document.createElement('article');
    row.className = 'dashboard-event';
    const title = document.createElement('strong');
    title.textContent = String(event.event_type || 'Unknown event').replaceAll('_', ' ');
    const time = document.createElement('time');
    time.textContent = formatTime(event.timestamp);
    const detail = document.createElement('span');
    detail.textContent = `${event.source || 'Unknown source'} · ${event.parameter || 'system'} · ${event.old_value ?? '--'} → ${event.new_value ?? '--'} · ${event.severity || 'unclassified'}`;
    row.append(title, time, detail);
    list.append(row);
  });
}

async function refreshDashboard() {
  const results = await Promise.allSettled([
    getJson('/api/telemetry'),
    getJson('/api/events'),
    getJson('/api/connection'),
    getJson('/api/state'),
  ]);

  const [telemetryResult, eventsResult, connectionResult, stateResult] = results;
  if (telemetryResult.status === 'fulfilled') {
    renderTelemetry(telemetryResult.value);
  } else {
    $('telemetry-time').textContent = 'TELEMETRY UNAVAILABLE';
    console.error('Could not load machine telemetry:', telemetryResult.reason);
  }

  if (eventsResult.status === 'fulfilled') {
    renderEvents(eventsResult.value);
  } else {
    setUnavailable('event-list', 'Machine events unavailable.');
    console.error('Could not load machine events:', eventsResult.reason);
  }

  if (connectionResult.status === 'fulfilled') {
    const connection = connectionResult.value;
    $('plc-status').textContent = connection.status || 'UNKNOWN';
    $('machine-state').textContent = connection.error || `${connection.host}:${connection.port}`;
  } else {
    $('plc-status').textContent = 'STATUS UNAVAILABLE';
    $('machine-state').textContent = connectionResult.reason.message;
  }

  if (stateResult.status === 'fulfilled') {
    $('machine-state').textContent = `${stateResult.value.motor_status} · target ${formatNumber(stateResult.value.target_rpm, 0)} RPM`;
  } else if (connectionResult.status === 'fulfilled') {
    $('machine-state').textContent = connectionResult.value.error || 'PLC is disconnected; stored history remains available.';
  }

  $('refresh-status').textContent = `UPDATED ${new Date().toLocaleTimeString()}`;
}

refreshDashboard();
window.setInterval(refreshDashboard, 2000);
