// 퍼널 분석용 PostHog 래퍼.
// PostHog가 로드되지 않은 환경(키 미설정·차단·로컬 개발)에서도 호출부가 깨지지 않도록 no-op 폴백.
(function (w) {
  function track(event, props) {
    try {
      if (w.posthog && typeof w.posthog.capture === 'function') {
        w.posthog.capture(event, props || {});
      }
    } catch (_) { /* analytics는 절대 사용자 흐름을 막아선 안 됨 */ }
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

  // 퍼널 이벤트 이름 — 변경 시 PostHog 대시보드 funnel 정의도 같이 갱신해야 함.
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
