import {initializeAuth, isCloud, authenticated, signIn, signOut, request} from './auth.js';
const $ = id => document.getElementById(id);
let cases = [], companies = [], nextCursor = null, selectedId = null, loading = false, pendingUpload = null;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const label = value => value.replaceAll('_', ' ');
const companyName = id => companies.find(c => c.company_id === id)?.name || id;
const badge = value => `<span class="badge ${esc(value)}">${esc(label(value))}</span>`;
const basename = path => path.split(/[\\/]/).pop();
function message(text, error = false) {
  $('message').textContent = text;
  $('message').className = error ? 'error' : '';
  updateAuth();
}
function updateAuth() {
  if (!isCloud()) return;
  $('auth').hidden = false;
  $('auth').textContent = authenticated() ? 'Sign out' : 'Sign in';
  $('submit').disabled = !authenticated() || loading || !companies.length;
}
function render() {
  const query = $('search').value.toLowerCase(), status = $('filter').value;
  const visible = cases.filter(c => (!status || c.status === status) &&
    [c.reference_number, c.source_document, companyName(c.company_id)].join(' ').toLowerCase().includes(query));
  $('total').textContent = cases.length;
  $('active').textContent = cases.filter(c => c.status === 'active').length;
  $('review').textContent = cases.filter(c => c.status === 'needs_review').length;
  $('cases').innerHTML = visible.map(c => `<tr><td><button class="casebtn" data-id="${esc(c.job_id || c.case_id)}">${esc(c.reference_number)}</button><div class="muted">${esc(basename(c.source_document))}</div></td><td>${esc(companyName(c.company_id))}</td><td>${c.member_count ?? c.members?.length ?? '—'}</td><td>${badge(c.status)}</td></tr>`).join('');
  $('empty').hidden = visible.length > 0;
  $('empty').textContent = cases.length ? 'No cases match your filters.' : 'No cases yet. Upload a roster to get started.';
  $('more').hidden = !nextCursor;
}
async function load(append = false) {
  const d = await request('/api/cases' + (append && nextCursor ? '?' + new URLSearchParams({cursor: nextCursor}) : ''));
  cases = append ? [...cases, ...d.cases] : d.cases;
  cases.sort((a, b) => b.received_at.localeCompare(a.received_at));
  companies = d.companies;
  nextCursor = d.next_cursor || null;
  const previous = $('company').value;
  $('company').innerHTML = companies.map(c => `<option value="${esc(c.company_id)}">${esc(c.name)}</option>`).join('');
  $('company').value = companies.some(c => c.company_id === previous) ? previous :
    companies.some(c => c.company_id === 'CMP-DALISAY') ? 'CMP-DALISAY' : companies[0]?.company_id || '';
  if (!isCloud()) $('demoDate').textContent = d.demo_date;
  else if (!companies.length) message('No companies are assigned to your account. Ask your administrator for access.');
  render(); updateAuth();
}
function detail(d, scroll = true) {
  selectedId = d.job?.job_id || d.case?.case_id;
  const c = d.case, status = d.job?.status || c?.status || 'queued';
  $('details').hidden = false;
  if (!c) {
    $('details').innerHTML = `<h2>${esc(d.job.reference_number)}</h2>${badge(status)}<p class="muted">${status === 'failed' ? 'Workflow failed. Ask your administrator to inspect the execution, or submit a corrected roster.' : status === 'awaiting_upload' ? 'Upload was not completed. Choose the file and submit it again to create a new enrollment.' : 'The workflow is queued or processing. This view refreshes automatically.'}</p>`;
  } else {
    const nums = new Map(c.member_numbers.map(n => [n.row, n]));
    $('details').innerHTML = `<div class="detailhead"><div><div class="muted">CASE DETAILS</div><h2>${esc(c.reference_number)}</h2></div>${badge(status)}</div><div class="muted detailmeta">${esc(companyName(c.company_id))} · ${esc(basename(c.source_document))}<br>${isCloud() ? 'Intake' : 'Demo intake'}: ${esc(c.received_at)}${c.ack_id ? '<br>Insurer acknowledgment: ' + esc(c.ack_id) : ''}</div>${status === 'failed' ? '<p class="notice">Workflow failed before completion. Contact your administrator.</p>' : ''}${c.review_reason ? `<p class="notice">${esc(c.review_reason)}</p>` : ''}<h3>Validation findings</h3>${c.flags.length ? `<ul class="findings">${c.flags.map(f => `<li>${badge(f.severity)} ${f.row ? 'Row ' + f.row + ' · ' : ''}${esc(f.message)}<br><span class="muted">${esc(f.code)}</span></li>`).join('')}</ul>` : '<p class="muted">No validation issues found.</p>'}${c.unmapped_headers.length ? `<p class="notice">Unmapped columns: ${c.unmapped_headers.map(esc).join(', ')}</p>` : ''}<h3>Members</h3><div class="tablewrap"><table><thead><tr><th>Name / employee</th><th>Plan</th><th>Member number</th><th>Issued on</th></tr></thead><tbody>${c.members.map(m => `<tr><td>${esc([m.first_name, m.last_name].filter(Boolean).join(' ') || 'Missing name')}<div class="muted">${esc(m.employee_id || 'No employee ID')} · ${esc(m.relationship || 'Unspecified')}</div></td><td>${esc(m.plan_code || '—')}</td><td>${esc(nums.get(m.row)?.member_number || 'Pending')}</td><td>${esc(nums.get(m.row)?.issued_on || '—')}</td></tr>`).join('')}</tbody></table></div><h3>Event timeline</h3><ol class="timeline">${d.events.map(e => `<li>${esc(label(e.type.replaceAll('.', ' · ')))}<time>${esc(e.at)}</time></li>`).join('')}</ol>`;
  }
  if (scroll) $('details').scrollIntoView({behavior: 'smooth', block: 'start'});
}
const jsonPost = body => ({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
async function uploadCloud(file) {
  const company = $('company').value;
  if (!pendingUpload || pendingUpload.file !== file || pendingUpload.company !== company) {
    const grant = await request('/api/uploads', jsonPost({company_id: company, filename: file.name, size: file.size}));
    const form = new FormData();
    for (const [key, value] of Object.entries(grant.upload.fields)) form.append(key, value);
    form.append('file', file); // S3 requires the file field last.
    message('Uploading roster to private storage?');
    const response = await fetch(grant.upload.url, {method: 'POST', body: form});
    if (!response.ok) throw Error('Upload failed. Select your file and try again.');
    pendingUpload = {file, company, jobId: grant.job_id};
  }
  message('Validating the uploaded file and starting the workflow?');
  // A network retry with the same selected file reuses the existing workflow id.
  const result = await request('/api/cases', jsonPost({job_id: pendingUpload.jobId}));
  return request('/api/cases/' + result.job_id);
}
$('upload').addEventListener('submit', async event => {
  event.preventDefault();
  const file = $('file').files[0];
  if (!file) return;
  if (!file.size || file.size > 5 * 1024 * 1024) return message('Choose a nonempty file up to 5 MB.', true);
  loading = true; $('submit').disabled = true;
  message(isCloud() ? 'Preparing a secure upload…' : 'Validating roster and running the local workflow…');
  try {
    const d = isCloud() ? await uploadCloud(file) : await request('/api/cases?' + new URLSearchParams({company: $('company').value, filename: file.name}),
      {method: 'POST', headers: {'X-Enrollment-Desk': 'local', 'Content-Type': 'application/octet-stream'}, body: file});
    await load();
    if (d.job && !cases.some(c => c.job_id === d.job.job_id)) { cases.unshift(d.job); render(); }
    detail(d);
    message(isCloud() ? 'Workflow started. Status updates automatically every 10 seconds.' : 'Roster processed. Case is ' + label(d.case.status) + '.');
    $('file').value = '';
    pendingUpload = null;
  } catch (error) { message(error.message, true); }
  finally { loading = false; $('submit').disabled = false; updateAuth(); }
});
$('cases').addEventListener('click', async event => {
  const button = event.target.closest('[data-id]');
  if (!button) return;
  try { detail(await request('/api/cases/' + encodeURIComponent(button.dataset.id))); }
  catch (error) { message(error.message, true); }
});
$('search').addEventListener('input', render);
$('filter').addEventListener('change', render);
$('auth').addEventListener('click', () => { (authenticated() ? Promise.resolve(signOut()) : signIn()).catch(e => message(e.message, true)); });
$('refresh').addEventListener('click', () => load().catch(e => message(e.message, true)));
$('more').addEventListener('click', () => load(true).catch(e => message(e.message, true)));
async function boot() {
  $('submit').disabled = true;
  await initializeAuth();
  if (isCloud()) {
    $('workspace').textContent = 'AWS WORKSPACE';
    $('mode').textContent = 'Insurer simulator · AWS workflow';
    $('simulation').textContent = 'Authenticated workspace. Uploads run through the AWS workflow with real timestamps and insurer simulator acknowledgment waits. Only your cases and assigned companies are visible. Use synthetic data for this development environment.';
    $('footer').textContent = 'Enrollment Desk · AWS development workspace · Private S3 case storage.';
    $('countLabel').textContent = 'Cases on this page';
    $('refresh').hidden = false;
    updateAuth();
    if (!authenticated()) { $('empty').textContent = 'Sign in to view your enrollments.'; message('Sign in to access your assigned companies.'); return; }
  }
  await load();
  $('submit').disabled = false; updateAuth();
}
boot().catch(error => message(error.message, true));
setInterval(async () => {
  if (!isCloud() || !authenticated() || document.hidden || loading) { updateAuth(); return; }
  try {
    // Keep loaded pages stable; refresh snapshots individually when browsing multiple pages.
    for (const c of cases.filter(c => !['active', 'needs_review', 'failed', 'awaiting_upload'].includes(c.status))) {
      const d = await request('/api/cases/' + c.job_id);
      Object.assign(c, d.job);
      if (selectedId === c.job_id) detail(d, false);
    }
    render(); updateAuth();
  } catch (error) { message(error.message, true); }
}, 10000);
