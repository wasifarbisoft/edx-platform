"""
A utility class which wraps the RateLimitMixin 3rd party class to do bad request counting
which can be used for rate limiting
"""

from datetime import datetime, timedelta

from django.conf import settings
from ratelimitbackend.backends import RateLimitMixin


class RequestRateLimiter(RateLimitMixin):
    """
    Use the 3rd party RateLimitMixin to help do rate limiting.
    """
    # get max number value from settings instead of using default one
    requests = settings.RATE_LIMIT_BACKEND_MAX_REQUESTS

    def is_rate_limit_exceeded(self, request):
        """
        Returns if the client has been rated limited
        """
        counts = self.get_counters(request)
        return sum(counts.values()) >= self.requests

    def tick_request_counter(self, request):
        """
        Ticks any counters used to compute when rate limt has been reached
        """
        self.cache_incr(self.get_cache_key(request))


class BadRequestRateLimiter(RequestRateLimiter):
    """
    Default rate limit is 30 requests for every 5 minutes.
    """
    pass


class PasswordResetEmailRateLimiter(RequestRateLimiter):
    """
    Rate limiting requests to send password reset emails.
    """
    email_rate_limit = getattr(settings, 'PASSWORD_RESET_EMAIL_RATE_LIMIT', {})
    requests = email_rate_limit.get('no_of_emails', 1)
    cache_timeout_seconds = email_rate_limit.get('per_seconds', 60)
    reset_email_cache_prefix = 'resetemail'

    def key(self, request, dt):
        """
        Returns IP based cache key.
        """
        return '%s-%s-%s' % (
            self.reset_email_cache_prefix,
            self.get_ip(request),
            dt.strftime('%Y%m%d%H%M'),
        )

    def email_key(self, request, dt):
        """
        Returns email based cache key.
        """
        return '%s-%s-%s' % (
            self.reset_email_cache_prefix,
            self.get_email(request),
            dt.strftime('%Y%m%d%H%M'),
        )

    def expire_after(self):
        """
        Returns timeout for cache keys.
        """
        return self.cache_timeout_seconds

    def get_email(self, request):
        """
        Returns email id for cache key.
        """
        user = request.user
        # Prefer logged-in user's email
        email = user.email if user.is_authenticated else request.POST.get('email')
        return email

    def keys_to_check(self, request):
        """
        Return list of IP and email based keys.
        """
        keys = super(PasswordResetEmailRateLimiter, self).keys_to_check(request)

        now = datetime.now()
        email_keys = [
            self.email_key(
                request,
                now - timedelta(minutes=minute),
            ) for minute in range(self.minutes + 1)
        ]
        keys.extend(email_keys)

        return keys

    def tick_request_counter(self, request):
        """
        Ticks any counters used to compute when rate limit has been reached.
        """
        for key in self.keys_to_check(request):
            self.cache_incr(key)
