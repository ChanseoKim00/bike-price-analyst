// PostHog wrapper for funnel analytics.
// Falls back to a no-op when PostHog isn't loaded (key not set, ad-blocked, local dev),
// so callers never break.
(function (w) {
  function track(event, props) {
    try {
      if (w.posthog && typeof w.posthog.capture === 'function') {
        w.posthog.capture(event, props || {});
      }
    } catch (_) { /* analytics must never block the user flow */ }
  }

  function identify(userId, props) {
    try {
      if (w.posthog && typeof w.posthog.identify === 'function' && userId) {
        w.posthog.identify(String(userId), props || {});
      }
    } catch (_) { /* noop */ }
  }

  function reset() {
    try {
      if (w.posthog && typeof w.posthog.reset === 'function') {
        w.posthog.reset();
      }
    } catch (_) { /* noop */ }
  }

  w.bpa = w.bpa || {};
  w.bpa.track    = track;
  w.bpa.identify = identify;
  w.bpa.reset    = reset;

  // Funnel event names — keep these in sync with the PostHog dashboard funnel definitions
  // whenever you change them.
  w.bpa.events = {
    MAIN_VIEW:           'funnel_main_view',
    URL_INPUT:           'funnel_url_input',
    ANALYZE_SUBMIT:      'funnel_analyze_submit',
    RESULT_VIEW:         'funnel_result_view',
    PRICING_VIEW:        'funnel_pricing_view',
    PLAN_SELECT:         'funnel_plan_select',
    CHECKOUT_VIEW:       'funnel_checkout_view',
    PAYMENT_ATTEMPT:     'funnel_payment_attempt',
    SIGNUP_COMPLETE:     'funnel_signup_complete',
    SUBSCRIBE_COMPLETE:  'funnel_subscribe_complete',
  };
})(window);
