// assets/dataset/metais_dup.js

/** 
 * @param {HTMLElement} container - where to render
 * @param {Object} data           - JSON loaded from data/<date>/dataset/metais_dup.json
 * @param {Object} ctx            - { date, category, instance, displayName }
 */
export function render(container, data, ctx) {
  // Safety: clear any previous content if re-used
  container.innerHTML = '';

  const title = document.createElement('h3');
  title.className = 'report-title';
  title.textContent = data.name || ctx.displayName || ctx.instance;

  const summary = document.createElement('p');
  summary.className = 'report-summary';
  summary.textContent = `Snapshot ${ctx.date}, ${data.count} duplicate groups.`;

  container.appendChild(title);
  container.appendChild(summary);

  // Example: simple list of groups
  const list = document.createElement('ul');
  list.className = 'dup-group-list';

  (data.groups || []).forEach(group => {
    const li = document.createElement('li');
    li.className = 'dup-group-item';

    const code = group.metais_code || group.code || '(no code)';
    const count = (group.entities || []).length;

    li.textContent = `${code} – ${count} entities`;
    list.appendChild(li);
  });

  container.appendChild(list);
}