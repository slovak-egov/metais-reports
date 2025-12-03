export function createToggleRow(parent, labelText, initial, onToggle) {
  const row = document.createElement('div');
  row.className = 'physics-row physics-row-toggle';

  const label = document.createElement('div');
  label.className = 'physics-label';
  label.textContent = labelText;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'app-header-chip app-header-chip-utility physics-toggle';

  function updateLabel(val) {
    btn.textContent = val ? 'On' : 'Off';
    if (val) {
      btn.classList.add('app-header-chip-active');
    } else {
      btn.classList.remove('app-header-chip-active');
    }
  }

  let state = !!initial;
  updateLabel(state);

  btn.addEventListener('click', () => {
    state = !state;
    updateLabel(state);
    onToggle(state);
  });

  row.appendChild(label);
  row.appendChild(btn);
  parent.appendChild(row);

  return { row, button: btn };
}

export function createSliderRow(
  parent,
  {
    labelText,
    min,
    max,
    step,
    initial,
    decimals = 2,
    onChange,
    scale = 'linear',
  }
) {

  if (scale === 'log' && (min <= 0 || max <= 0)) {
    console.warn(
      `[createSliderRow] Log scale requested for "${labelText}" but min/max <= 0; falling back to linear.`
    );
    scale = 'linear';
  }

  const row = document.createElement('div');
  row.className = 'physics-row physics-row-slider';

  const label = document.createElement('div');
  label.className = 'physics-label';
  label.textContent = labelText;

  const sliderWrap = document.createElement('div');
  sliderWrap.className = 'physics-slider-wrap';

  const input = document.createElement('input');
  input.type = 'range';
  input.className = 'physics-slider';

  const valueEl = document.createElement('div');
  valueEl.className = 'physics-value';

  let logMin = null;
  let logMax = null;

  function fromSlider(raw) {
    if (scale === 'log') {
      const t = raw / 1000;
      const val = Math.exp(logMin + t * (logMax - logMin));
      return val;
    }
    // linear
    return raw;
  }

  function toSlider(val) {
    if (scale === 'log') {
      const v = Math.min(Math.max(val, min), max);
      const t = (Math.log(v) - logMin) / (logMax - logMin);
      return t * 1000;
    }
    // linear
    return val;
  }

  // ---- Configure input range & initial value ----
  let displayInitial = initial;

  if (scale === 'log') {
    // internal slider always 0..1000 for log
    input.min = '0';
    input.max = '1000';
    input.step = '1';

    logMin = Math.log(min);
    logMax = Math.log(max);

    if (displayInitial == null || displayInitial <= 0) {
      displayInitial = min;
    }
    displayInitial = Math.min(Math.max(displayInitial, min), max);
    input.value = String(Math.round(toSlider(displayInitial)));
  } else {
    // original linear behavior
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    if (displayInitial == null) displayInitial = min;
    displayInitial = Math.min(Math.max(displayInitial, min), max);
    input.value = String(displayInitial);
  }

  valueEl.textContent = Number(displayInitial).toFixed(decimals);

  input.addEventListener('input', () => {
    let raw = Number(input.value);
    let val;

    if (scale === 'log') {
      val = fromSlider(raw);
    } else {
      val = raw;
    }

    const rounded = Number(val.toFixed(decimals));
    valueEl.textContent = rounded.toFixed(decimals);
    onChange(rounded);
  });

  sliderWrap.appendChild(input);
  sliderWrap.appendChild(valueEl);

  row.appendChild(label);
  row.appendChild(sliderWrap);
  parent.appendChild(row);

  return { row, input, valueEl };
}