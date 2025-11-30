/**
 * Creates a standard header section with:
 *   <div class="dup-header-option dup-header-option--{size}">
 *     <div class="dup-header-option-header {extraHeaderClass}">
 *       <div class="app-header-chip-label">label</div>
 *       <div class="section-controls"></div>
 *     </div>
 *     <div class="app-header-chip-shell">
 *       <div class="app-header-chip-container"></div>
 *     </div>
 *   </div>
 *
 * Returns { section, headerRow, labelEl, controlsEl, shellEl, chipContainer }.
 */
export function createChipSection(parent, {
  labelText,
  sizeClass = 'dup-header-option--medium', // or --wide
  extraHeaderClass = '',
}) {
  const section = document.createElement('div');
  section.className = `dup-header-option ${sizeClass}`;
  parent.appendChild(section);

  const headerRow = document.createElement('div');
  headerRow.className = `dup-header-option-header ${extraHeaderClass}`.trim();
  section.appendChild(headerRow);

  const labelEl = document.createElement('div');
  labelEl.className = 'app-header-chip-label';
  labelEl.textContent = labelText;
  headerRow.appendChild(labelEl);

  const controlsEl = document.createElement('div');
  controlsEl.className = 'section-controls';
  headerRow.appendChild(controlsEl);

  const shellEl = document.createElement('div');
  shellEl.className = 'app-header-chip-shell';
  section.appendChild(shellEl);

  const chipContainer = document.createElement('div');
  chipContainer.className = 'app-header-chip-container';
  shellEl.appendChild(chipContainer);

  return { section, headerRow, labelEl, controlsEl, shellEl, chipContainer };
}