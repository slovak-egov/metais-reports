// Simple On/Off chip-style toggle row
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

// Slider row with label + live value
export function createSliderRow(parent, {
  labelText,
  min,
  max,
  step,
  initial,
  decimals = 2,
  onChange,
}) {
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
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = String(initial);

  const valueEl = document.createElement('div');
  valueEl.className = 'physics-value';
  valueEl.textContent = Number(initial).toFixed(decimals);

  input.addEventListener('input', () => {
    const v = Number(input.value);
    valueEl.textContent = v.toFixed(decimals);
    onChange(v);
  });

  sliderWrap.appendChild(input);
  sliderWrap.appendChild(valueEl);

  row.appendChild(label);
  row.appendChild(sliderWrap);
  parent.appendChild(row);

  return { row, input, valueEl };
}