export function renderControlsRow(controlsSpec, controlsEl) {
  controlsEl.innerHTML = '';

  controlsSpec.forEach(control => {
    if (control.type === 'button') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'app-header-chip app-header-chip-utility';
      btn.textContent = control.label;
      btn.addEventListener('click', control.onClick);
      controlsEl.appendChild(btn);
      return;
    }

    if (control.type === 'toggle') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'app-header-chip app-header-chip-utility';

      const sync = () => {
        const val = !!control.get();
        btn.textContent = val ? control.labelOn : control.labelOff;
        btn.classList.toggle('app-header-chip-active', val);
      };

      btn.addEventListener('click', () => {
        const newVal = !control.get();
        control.set(newVal);
        sync();
      });

      sync();
      controlsEl.appendChild(btn);
      return;
    }

    if (control.type === 'chipGroup') {
      const wrap = document.createElement('div');
      wrap.className = 'hub-layer-chip-wrap';

      if (control.label) {
        const label = document.createElement('span');
        label.className = 'hub-layers-label';
        label.textContent = control.label;
        wrap.appendChild(label);
      }

      const items = control.items();

      items.forEach(item => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className =
          'app-header-chip app-header-chip-utility hub-layer-chip';
        chip.textContent = String(item);

        const syncActive = () => {
          if (control.isActive(item)) {
            chip.classList.add('app-header-chip-active');
          } else {
            chip.classList.remove('app-header-chip-active');
          }
        };

        chip.addEventListener('click', () => {
          control.onSelect(item);
          // re-sync whole group after state change
          renderControlsRow(controlsSpec, controlsEl);
        });

        syncActive();
        wrap.appendChild(chip);
      });

      controlsEl.appendChild(wrap);
    }
  });
}