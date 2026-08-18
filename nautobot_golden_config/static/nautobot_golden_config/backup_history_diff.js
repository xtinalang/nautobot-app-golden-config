/*
 * Backup History Diff -- progressive enhancement for the two commit selectors.
 *
 * The selectors are a plain GET form with a "Compare" submit button, so choosing two commits works
 * with JavaScript disabled. This script only removes the extra click: it hides the button and
 * submits the form as soon as either dropdown changes. If it never runs, the form still works.
 */
document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("gcd-compare-form");
    if (!form) {
        return;
    }

    var submitButton = form.querySelector(".gcd-compare-submit");
    if (submitButton) {
        submitButton.hidden = true;
    }

    form.querySelectorAll("select").forEach(function (select) {
        select.addEventListener("change", function () {
            form.submit();
        });
    });
});
