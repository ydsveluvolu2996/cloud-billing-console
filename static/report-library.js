// Selection is kept on the current authorized page and submitted to scoped views.
const reportSelections = [...document.querySelectorAll('[data-library-select]')];
const selectAllReports = document.querySelector('[data-library-select-all]');
function updateReportSelection() {
  const selected = reportSelections.filter(input => input.checked).length;
  document.querySelectorAll('[data-library-action]').forEach(button => { button.disabled = selected === 0; });
  const status = document.querySelector('[data-library-selection]');
  if (status) status.textContent = `${selected} report${selected === 1 ? '' : 's'} selected`;
  if (selectAllReports) {
    selectAllReports.checked = reportSelections.length > 0 && selected === reportSelections.length;
    selectAllReports.indeterminate = selected > 0 && selected < reportSelections.length;
    selectAllReports.disabled = reportSelections.length === 0;
  }
}
reportSelections.forEach(input => input.addEventListener('change', updateReportSelection));
selectAllReports?.addEventListener('change', () => {
  reportSelections.forEach(input => { input.checked = selectAllReports.checked; });
  updateReportSelection();
});
updateReportSelection();
