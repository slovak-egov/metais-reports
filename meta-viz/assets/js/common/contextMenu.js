let activeCleanup = null;  // holds cleanup function for the currently open menu

export function closeContextMenu() {
  if (activeCleanup) {
    activeCleanup();
    activeCleanup = null;
  }
}

export function showContextMenu(container, event, items) {
  // container is now only for “context” (if you ever need it), not for layout
  event.preventDefault();
  event.stopPropagation(); // <- don't let this bubble up and instantly close us

  // close previous menu if any
  closeContextMenu();

  const menu = document.createElement('div');
  menu.className = 'graph-context-menu';
  menu.style.position = 'fixed';
  menu.style.left = `${event.clientX}px`;
  menu.style.top  = `${event.clientY}px`;
  menu.style.zIndex = '9999';    // make *really* sure it's above the canvas

  items.forEach(item => {
    if (item.type === 'separator') {
      const sep = document.createElement('div');
      sep.className = 'graph-context-menu-separator';
      menu.appendChild(sep);
      return;
    }

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'graph-context-menu-item';
    btn.textContent = item.label;

    btn.addEventListener('click', e => {
      e.stopPropagation();
      closeContextMenu();
      if (item.onClick) {
        item.onClick();
      }
    });

    menu.appendChild(btn);
  });

  function cleanup() {
    if (menu.parentNode) {
      menu.parentNode.removeChild(menu);
    }
    document.removeEventListener('click', onDocClick, true);
    document.removeEventListener('contextmenu', onDocClick, true);
    activeCleanup = null;
  }

  function onDocClick(e) {
    // click / right-click outside → close
    if (!menu.contains(e.target)) {
      cleanup();
    }
  }

  // use capture so this wins over other bubbling handlers if needed
  document.addEventListener('click', onDocClick, true);
  document.addEventListener('contextmenu', onDocClick, true);

  // *** important change: append to body, not container ***
  document.body.appendChild(menu);

  activeCleanup = cleanup;
}