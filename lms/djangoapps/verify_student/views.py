"""
Views for the verification flow
"""


import datetime
import decimal
import json
import logging

import six
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.timezone import now
from django.utils.translation import ugettext as _
from django.utils.translation import ugettext_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic.base import View
from edx_rest_api_client.exceptions import SlumberBaseException
from ipware.ip import get_ip
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework.views import APIView
from rest_framework.response import Response

from course_modes.models import CourseMode
from edxmako.shortcuts import render_to_response, render_to_string
from lms.djangoapps.commerce.utils import EcommerceService, is_account_activation_requirement_disabled
from lms.djangoapps.verify_student.image import InvalidImageData, decode_image_data
from lms.djangoapps.verify_student.models import SoftwareSecurePhotoVerification, VerificationDeadline
from lms.djangoapps.verify_student.services import IDVerificationService
from lms.djangoapps.verify_student.ssencrypt import has_valid_signature
from lms.djangoapps.verify_student.tasks import send_verification_status_email
from lms.djangoapps.verify_student.utils import can_verify_now
from openedx.core.djangoapps.commerce.utils import ecommerce_api_client
from openedx.core.djangoapps.embargo import api as embargo_api
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.user_api.accounts import NAME_MIN_LENGTH
from openedx.core.djangoapps.user_api.accounts.api import update_account_settings
from openedx.core.djangoapps.user_api.errors import AccountValidationError, UserNotFound
from openedx.core.lib.log_utils import audit_log
from shoppingcart.models import CertificateItem, Order
from shoppingcart.processors import get_purchase_endpoint, get_signed_purchase_params
from student.models import CourseEnrollment
from track import segment
from util.db import outer_atomic
from util.json_request import JsonResponse
from xmodule.modulestore.django import modulestore

log = logging.getLogger(__name__)


class PayAndVerifyView(View):
    """
    View for the "verify and pay" flow.

    This view is somewhat complicated, because the user
    can enter it from a number of different places:

    * From the "choose your track" page.
    * After completing payment.
    * From the dashboard in order to complete verification.
    * From the dashboard in order to upgrade to a verified track.

    The page will display different steps and requirements
    depending on:

    * Whether the user has submitted a photo verification recently.
    * Whether the user has paid for the course.
    * How the user reached the page (mostly affects messaging)

    We are also super-paranoid about how users reach this page.
    If they somehow aren't enrolled, or the course doesn't exist,
    or they've unenrolled, or they've already paid/verified,
    ... then we try to redirect them to the page with the
    most appropriate messaging (including the dashboard).

    Note that this page does NOT handle re-verification
    (photo verification that was denied or had an error);
    that is handled by the "reverify" view.

    """

    # Step definitions
    #
    # These represent the numbered steps a user sees in
    # the verify / payment flow.
    #
    # Steps can either be:
    # - displayed or hidden
    # - complete or incomplete
    #
    # For example, when a user enters the verification/payment
    # flow for the first time, the user will see steps
    # for both payment and verification.  As the user
    # completes these steps (for example, submitting a photo)
    # the steps will be marked "complete".
    #
    # If a user has already verified for another course,
    # then the verification steps will be hidden,
    # since the user has already completed them.
    #
    # If a user re-enters the flow from another application
    # (for example, after completing payment through
    # a third-party payment processor), then the user
    # will resume the flow at an intermediate step.
    #
    INTRO_STEP = 'intro-step'
    MAKE_PAYMENT_STEP = 'make-payment-step'
    PAYMENT_CONFIRMATION_STEP = 'payment-confirmation-step'
    FACE_PHOTO_STEP = 'face-photo-step'
    ID_PHOTO_STEP = 'id-photo-step'
    REVIEW_PHOTOS_STEP = 'review-photos-step'
    ENROLLMENT_CONFIRMATION_STEP = 'enrollment-confirmation-step'

    ALL_STEPS = [
        INTRO_STEP,
        MAKE_PAYMENT_STEP,
        PAYMENT_CONFIRMATION_STEP,
        FACE_PHOTO_STEP,
        ID_PHOTO_STEP,
        REVIEW_PHOTOS_STEP,
        ENROLLMENT_CONFIRMATION_STEP
    ]

    PAYMENT_STEPS = [
        MAKE_PAYMENT_STEP,
        PAYMENT_CONFIRMATION_STEP
    ]

    VERIFICATION_STEPS = [
        FACE_PHOTO_STEP,
        ID_PHOTO_STEP,
        REVIEW_PHOTOS_STEP,
        ENROLLMENT_CONFIRMATION_STEP
    ]

    # These steps can be skipped using the ?skip-first-step GET param
    SKIP_STEPS = [
        INTRO_STEP,
    ]

    STEP_TITLES = {
        INTRO_STEP: ugettext_lazy("Intro"),
        MAKE_PAYMENT_STEP: ugettext_lazy("Make payment"),
        PAYMENT_CONFIRMATION_STEP: ugettext_lazy("Payment confirmation"),
        FACE_PHOTO_STEP: ugettext_lazy("Take photo"),
        ID_PHOTO_STEP: ugettext_lazy("Take a photo of your ID"),
        REVIEW_PHOTOS_STEP: ugettext_lazy("Review your info"),
        ENROLLMENT_CONFIRMATION_STEP: ugettext_lazy("Enrollment confirmation"),
    }

    # Messages
    #
    # Depending on how the user entered reached the page,
    # we will display different text messaging.
    # For example, we show users who are upgrading
    # slightly different copy than users who are verifying
    # for the first time.
    #
    FIRST_TIME_VERIFY_MSG = 'first-time-verify'
    VERIFY_NOW_MSG = 'verify-now'
    VERIFY_LATER_MSG = 'verify-later'
    UPGRADE_MSG = 'upgrade'
    PAYMENT_CONFIRMATION_MSG = 'payment-confirmation'

    # Requirements
    #
    # These explain to the user what he or she
    # will need to successfully pay and/or verify.
    #
    # These are determined by the steps displayed
    # to the user; for example, if the user does not
    # need to complete the verification steps,
    # then the photo ID and webcam requirements are hidden.
    #
    ACCOUNT_ACTIVATION_REQ = "account-activation-required"
    PHOTO_ID_REQ = "photo-id-required"
    WEBCAM_REQ = "webcam-required"

    STEP_REQUIREMENTS = {
        ID_PHOTO_STEP: [PHOTO_ID_REQ, WEBCAM_REQ],
        FACE_PHOTO_STEP: [WEBCAM_REQ],
    }

    # Deadline types
    VERIFICATION_DEADLINE = "verification"
    UPGRADE_DEADLINE = "upgrade"

    def _get_user_active_status(self, user):
        """
        Returns the user's active status to the caller
        Overrides the actual value if account activation has been disabled via waffle switch

        Arguments:
            user (User): Current user involved in the onboarding/verification flow
        """
        return user.is_active or is_account_activation_requirement_disabled()

    @method_decorator(login_required)
    def get(
        self, request, course_id,
        always_show_payment=False,
        current_step=None,
        message=FIRST_TIME_VERIFY_MSG
    ):
        """
        Render the payment and verification flow.

        Arguments:
            request (HttpRequest): The request object.
            course_id (unicode): The ID of the course the user is trying
                to enroll in.

        Keyword Arguments:
            always_show_payment (bool): If True, show the payment steps
                even if the user has already paid.  This is useful
                for users returning to the flow after paying.
            current_step (string): The current step in the flow.
            message (string): The messaging to display.

        Returns:
            HttpResponse

        Raises:
            Http404: The course does not exist or does not
                have a verified mode.

        """
        # Parse the course key
        # The URL regex should guarantee that the key format is valid.
        course_key = CourseKey.from_string(course_id)
        course = modulestore().get_course(course_key)

        # Verify that the course exists
        if course is None:
            log.warning(u"Could not find course with ID %s.", course_id)
            raise Http404

        # Check whether the user has access to this course
        # based on country access rules.
        redirect_url = embargo_api.redirect_if_blocked(
            course_key,
            user=request.user,
            ip_address=get_ip(request),
            url=request.path
        )
        if redirect_url:
            return redirect(redirect_url)

        # If the verification deadline has passed
        # then show the user a message that he/she can't verify.
        #
        # We're making the assumptions (enforced in Django admin) that:
        #
        # 1) Only verified modes have verification deadlines.
        #
        # 2) If set, verification deadlines are always AFTER upgrade deadlines, because why would you
        #   let someone upgrade into a verified track if they can't complete verification?
        #
        verification_deadline = VerificationDeadline.deadline_for_course(course.id)
        response = self._response_if_deadline_passed(course, self.VERIFICATION_DEADLINE, verification_deadline)
        if response is not None:
            log.info(u"Verification deadline for '%s' has passed.", course.id)
            return response

        # Retrieve the relevant course mode for the payment/verification flow.
        #
        # WARNING: this is technical debt!  A much better way to do this would be to
        # separate out the payment flow and use the product SKU to figure out what
        # the user is trying to purchase.
        #
        # Nonetheless, for the time being we continue to make the really ugly assumption
        # that at some point there was a paid course mode we can query for the price.
        relevant_course_mode = self._get_paid_mode(course_key)

        # If we can find a relevant course mode, then log that we're entering the flow
        # Otherwise, this course does not support payment/verification, so respond with a 404.
        if relevant_course_mode is not None:
            if CourseMode.is_verified_mode(relevant_course_mode):
                log.info(
                    u"Entering payment and verification flow for user '%s', course '%s', with current step '%s'.",
                    request.user.id, course_id, current_step
                )
            else:
                log.info(
                    u"Entering payment flow for user '%s', course '%s', with current step '%s'",
                    request.user.id, course_id, current_step
                )
        else:
            # Otherwise, there has never been a verified/paid mode,
            # so return a page not found response.
            log.warning(
                u"No paid/verified course mode found for course '%s' for verification/payment flow request",
                course_id
            )
            raise Http404

        # If the user is trying to *pay* and the upgrade deadline has passed,
        # then they shouldn't be able to enter the flow.
        #
        # NOTE: This should match the availability dates used by the E-Commerce service
        # to determine whether a user can purchase a product.  The idea is that if the service
        # won't fulfill the order, we shouldn't even let the user get into the payment flow.
        #
        user_is_trying_to_pay = message in [self.FIRST_TIME_VERIFY_MSG, self.UPGRADE_MSG]
        if user_is_trying_to_pay:
            upgrade_deadline = relevant_course_mode.expiration_datetime
            response = self._response_if_deadline_passed(course, self.UPGRADE_DEADLINE, upgrade_deadline)
            if response is not None:
                log.info(u"Upgrade deadline for '%s' has passed.", course.id)
                return response

        # Check whether the user has verified, paid, and enrolled.
        # A user is considered "paid" if he or she has an enrollment
        # with a paid course mode (such as "verified").
        # For this reason, every paid user is enrolled, but not
        # every enrolled user is paid.
        # If the course mode is not verified(i.e only paid) then already_verified is always True
        already_verified = (
            self._check_already_verified(request.user)
            if CourseMode.is_verified_mode(relevant_course_mode)
            else True
        )
        already_paid, is_enrolled = self._check_enrollment(request.user, course_key)

        # Redirect the user to a more appropriate page if the
        # messaging won't make sense based on the user's
        # enrollment / payment / verification status.
        sku_to_use = relevant_course_mode.sku
        purchase_workflow = request.GET.get('purchase_workflow', 'single')
        if purchase_workflow == 'bulk' and relevant_course_mode.bulk_sku:
            sku_to_use = relevant_course_mode.bulk_sku
        redirect_response = self._redirect_if_necessary(
            message,
            already_verified,
            already_paid,
            is_enrolled,
            course_key,
            user_is_trying_to_pay,
            request.user,
            sku_to_use
        )
        if redirect_response is not None:
            return redirect_response

        display_steps = self._display_steps(
            always_show_payment,
            already_verified,
            already_paid,
            relevant_course_mode
        )

        # Override the actual value if account activation has been disabled
        # Also see the reference to this parameter in context dictionary further down
        user_is_active = self._get_user_active_status(request.user)
        requirements = self._requirements(display_steps, user_is_active)

        if current_step is None:
            current_step = display_steps[0]['name']

        # Allow the caller to skip the first page
        # This is useful if we want the user to be able to
        # use the "back" button to return to the previous step.
        # This parameter should only work for known skip-able steps
        if request.GET.get('skip-first-step') and current_step in self.SKIP_STEPS:
            display_step_names = [step['name'] for step in display_steps]
            current_step_idx = display_step_names.index(current_step)
            if (current_step_idx + 1) < len(display_steps):
                current_step = display_steps[current_step_idx + 1]['name']

        courseware_url = ""
        if not course.start or course.start < now():
            courseware_url = reverse(
                'course_root',
                kwargs={'course_id': six.text_type(course_key)}
            )

        full_name = (
            request.user.profile.name
            if request.user.profile.name
            else ""
        )

        # If the user set a contribution amount on another page,
        # use that amount to pre-fill the price selection form.
        contribution_amount = request.session.get(
            'donation_for_course', {}
        ).get(six.text_type(course_key), '')

        # Remember whether the user is upgrading
        # so we can fire an analytics event upon payment.
        request.session['attempting_upgrade'] = (message == self.UPGRADE_MSG)

        # Determine the photo verification status
        verification_good_until = self._verification_valid_until(request.user)

        # get available payment processors
        if relevant_course_mode.sku:
            # transaction will be conducted via ecommerce service
            processors = ecommerce_api_client(request.user).payment.processors.get()
        else:
            # transaction will be conducted using legacy shopping cart
            processors = [settings.CC_PROCESSOR_NAME]

        # Render the top-level page
        context = {
            'contribution_amount': contribution_amount,
            'course': course,
            'course_key': six.text_type(course_key),
            'checkpoint_location': request.GET.get('checkpoint'),
            'course_mode': relevant_course_mode,
            'courseware_url': courseware_url,
            'current_step': current_step,
            'disable_courseware_js': True,
            'display_steps': display_steps,
            'is_active': json.dumps(user_is_active),
            'user_email': request.user.email,
            'message_key': message,
            'platform_name': configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME),
            'processors': processors,
            'requirements': requirements,
            'user_full_name': full_name,
            'verification_deadline': verification_deadline or "",
            'already_verified': already_verified,
            'verification_good_until': verification_good_until,
            'capture_sound': staticfiles_storage.url("audio/camera_capture.wav"),
            'nav_hidden': True,
            'is_ab_testing': 'begin-flow' in request.path,
        }

        return render_to_response("verify_student/pay_and_verify.html", context)

    def add_utm_params_to_url(self, url):
        # utm_params is [(u'utm_content', u'course-v1:IDBx IDB20.1x 1T2017'),...
        utm_params = [item for item in self.request.GET.items() if 'utm_' in item[0]]
        # utm_params is utm_content=course-v1%3AIDBx+IDB20.1x+1T2017&...
        utm_params = six.moves.urllib.parse.urlencode(utm_params, True)
        # utm_params is utm_content=course-v1:IDBx+IDB20.1x+1T2017&...
        # (course-keys do not have url encoding)
        utm_params = six.moves.urllib.parse.unquote(utm_params)
        if utm_params:
            if '?' in url:
                url = url + '&' + utm_params
            else:
                url = url + '?' + utm_params
        return url

    def _redirect_if_necessary(
        self, message, already_verified, already_paid, is_enrolled, course_key,
        user_is_trying_to_pay, user, sku
    ):
        """Redirect the user to a more appropriate page if necessary.

        In some cases, a user may visit this page with
        verification / enrollment / payment state that
        we don't anticipate.  For example, a user may unenroll
        from the course after paying for it, then visit the
        "verify now" page to complete verification.

        When this happens, we try to redirect the user to
        the most appropriate page.

        Arguments:

            message (string): The messaging of the page.  Should be a key
                in `MESSAGES`.

            already_verified (bool): Whether the user has submitted
                a verification request recently.

            already_paid (bool): Whether the user is enrolled in a paid
                course mode.

            is_enrolled (bool): Whether the user has an active enrollment
                in the course.

            course_key (CourseKey): The key for the course.

        Returns:
            HttpResponse or None

        """
        url = None
        course_kwargs = {'course_id': six.text_type(course_key)}

        if already_verified and already_paid:
            # If they've already paid and verified, there's nothing else to do,
            # so redirect them to the dashboard.
            if message != self.PAYMENT_CONFIRMATION_MSG:
                url = reverse('dashboard')
        elif message in [self.VERIFY_NOW_MSG, self.VERIFY_LATER_MSG, self.PAYMENT_CONFIRMATION_MSG]:
            if is_enrolled:
                # If the user is already enrolled but hasn't yet paid,
                # then the "upgrade" messaging is more appropriate.
                if not already_paid:
                    url = reverse('verify_student_upgrade_and_verify', kwargs=course_kwargs)
            else:
                # If the user is NOT enrolled, then send him/her
                # to the first time verification page.
                url = reverse('verify_student_start_flow', kwargs=course_kwargs)
        elif message == self.UPGRADE_MSG:
            if is_enrolled:
                if already_paid:
                    # If the student has paid, but not verified, redirect to the verification flow.
                    url = reverse('verify_student_verify_now', kwargs=course_kwargs)
            else:
                url = reverse('verify_student_start_flow', kwargs=course_kwargs)

        if user_is_trying_to_pay and self._get_user_active_status(user) and not already_paid:
            # If the user is trying to pay, has activated their account, and the ecommerce service
            # is enabled redirect him to the ecommerce checkout page.
            ecommerce_service = EcommerceService()
            if ecommerce_service.is_enabled(user):
                url = ecommerce_service.get_checkout_page_url(
                    sku,
                    catalog=self.request.GET.get('catalog')
                )

        # Redirect if necessary, otherwise implicitly return None
        if url is not None:
            url = self.add_utm_params_to_url(url)
            return redirect(url)

    def _get_paid_mode(self, course_key):
        """
        Retrieve the paid course mode for a course.

        The returned course mode may or may not be expired.
        Unexpired modes are preferred to expired modes.

        Arguments:
            course_key (CourseKey): The location of the course.

        Returns:
            CourseMode tuple

        """
        # Retrieve all the modes at once to reduce the number of database queries
        all_modes, unexpired_modes = CourseMode.all_and_unexpired_modes_for_courses([course_key])

        # Retrieve the first mode that matches the following criteria:
        #  * Unexpired
        #  * Price > 0
        #  * Not credit
        for mode in unexpired_modes[course_key]:
            if mode.min_price > 0 and not CourseMode.is_credit_mode(mode):
                return mode

        # Otherwise, find the first non credit expired paid mode
        for mode in all_modes[course_key]:
            if mode.min_price > 0 and not CourseMode.is_credit_mode(mode):
                return mode

        # Otherwise, return None and so the view knows to respond with a 404.
        return None

    def _display_steps(self, always_show_payment, already_verified, already_paid, course_mode):
        """Determine which steps to display to the user.

        Includes all steps by default, but removes steps
        if the user has already completed them.

        Arguments:

            always_show_payment (bool): If True, display the payment steps
                even if the user has already paid.

            already_verified (bool): Whether the user has submitted
                a verification request recently.

            already_paid (bool): Whether the user is enrolled in a paid
                course mode.

        Returns:
            list

        """
        display_steps = self.ALL_STEPS
        remove_steps = set()

        if already_verified or not CourseMode.is_verified_mode(course_mode):
            remove_steps |= set(self.VERIFICATION_STEPS)

        if already_paid and not always_show_payment:
            remove_steps |= set(self.PAYMENT_STEPS)
        else:
            # The "make payment" step doubles as an intro step,
            # so if we're showing the payment step, hide the intro step.
            remove_steps |= set([self.INTRO_STEP])
        return [
            {
                'name': step,
                'title': six.text_type(self.STEP_TITLES[step]),
            }
            for step in display_steps
            if step not in remove_steps
        ]

    def _requirements(self, display_steps, is_active):
        """Determine which requirements to show the user.

        For example, if the user needs to submit a photo
        verification, tell the user that she will need
        a photo ID and a webcam.

        Arguments:
            display_steps (list): The steps to display to the user.
            is_active (bool): If False, adds a requirement to activate the user account.

        Returns:
            dict: Keys are requirement names, values are booleans
                indicating whether to show the requirement.

        """
        all_requirements = {
            self.ACCOUNT_ACTIVATION_REQ: not is_active,
            self.PHOTO_ID_REQ: False,
            self.WEBCAM_REQ: False,
        }

        # Remove the account activation requirement if disabled via waffle
        if is_account_activation_requirement_disabled():
            all_requirements.pop(self.ACCOUNT_ACTIVATION_REQ)

        display_steps = set(step['name'] for step in display_steps)

        for step, step_requirements in six.iteritems(self.STEP_REQUIREMENTS):
            if step in display_steps:
                for requirement in step_requirements:
                    all_requirements[requirement] = True

        return all_requirements

    def _verification_valid_until(self, user, date_format="%m/%d/%Y"):
        """
        Check whether the user has a valid or pending verification.

        Arguments:
            user:
            date_format: optional parameter for formatting datetime
                object to string in response

        Returns:
            datetime object in string format
        """
        expiration_datetime = IDVerificationService.get_expiration_datetime(
            user, ['submitted', 'approved', 'must_retry']
        )
        # return 'expiration_datetime' of latest photo verification if found,
        # otherwise implicitly return ''
        if expiration_datetime:
            return expiration_datetime.strftime(date_format)

        return ''

    def _check_already_verified(self, user):
        """Check whether the user has a valid or pending verification.

        Note that this includes cases in which the user's verification
        has not been accepted (either because it hasn't been processed,
        or there was an error).

        This should return True if the user has done their part:
        submitted photos within the expiration period.

        """
        return IDVerificationService.user_has_valid_or_pending(user)

    def _check_enrollment(self, user, course_key):
        """Check whether the user has an active enrollment and has paid.

        If a user is enrolled in a paid course mode, we assume
        that the user has paid.

        Arguments:
            user (User): The user to check.
            course_key (CourseKey): The key of the course to check.

        Returns:
            Tuple `(has_paid, is_active)` indicating whether the user
            has paid and whether the user has an active account.

        """
        enrollment_mode, is_active = CourseEnrollment.enrollment_mode_for_user(user, course_key)
        has_paid = False

        if enrollment_mode is not None and is_active:
            all_modes = CourseMode.modes_for_course_dict(course_key, include_expired=True)
            course_mode = all_modes.get(enrollment_mode)
            has_paid = (course_mode and course_mode.min_price > 0)

        return (has_paid, bool(is_active))

    def _response_if_deadline_passed(self, course, deadline_name, deadline_datetime):
        """
        Respond with some error messaging if the deadline has passed.

        Arguments:
            course (Course): The course the user is trying to enroll in.
            deadline_name (str): One of the deadline constants.
            deadline_datetime (datetime): The deadline.

        Returns: HttpResponse or None

        """
        if deadline_name not in [self.VERIFICATION_DEADLINE, self.UPGRADE_DEADLINE]:
            log.error(u"Invalid deadline name %s.  Skipping check for whether the deadline passed.", deadline_name)
            return None

        deadline_passed = (
            deadline_datetime is not None and
            deadline_datetime < now()
        )
        if deadline_passed:
            context = {
                'course': course,
                'deadline_name': deadline_name,
                'deadline': deadline_datetime
            }
            return render_to_response("verify_student/missed_deadline.html", context)


def checkout_with_ecommerce_service(user, course_key, course_mode, processor):
    """ Create a new basket and trigger immediate checkout, using the E-Commerce API. """
    course_id = six.text_type(course_key)
    try:
        api = ecommerce_api_client(user)
        # Make an API call to create the order and retrieve the results
        result = api.baskets.post({
            'products': [{'sku': course_mode.sku}],
            'checkout': True,
            'payment_processor_name': processor
        })

        # Pass the payment parameters directly from the API response.
        return result.get('payment_data')
    except SlumberBaseException:
        params = {'username': user.username, 'mode': course_mode.slug, 'course_id': course_id}
        log.exception(u'Failed to create order for %(username)s %(mode)s mode of %(course_id)s', params)
        raise
    finally:
        audit_log(
            'checkout_requested',
            course_id=course_id,
            mode=course_mode.slug,
            processor_name=processor,
            user_id=user.id
        )


def checkout_with_shoppingcart(request, user, course_key, course_mode, amount):
    """ Create an order and trigger checkout using shoppingcart."""
    cart = Order.get_cart_for_user(user)
    cart.clear()
    enrollment_mode = course_mode.slug
    CertificateItem.add_to_order(cart, course_key, amount, enrollment_mode)

    # Change the order's status so that we don't accidentally modify it later.
    # We need to do this to ensure that the parameters we send to the payment system
    # match what we store in the database.
    # (Ordinarily we would do this client-side when the user submits the form, but since
    # the JavaScript on this page does that immediately, we make the change here instead.
    # This avoids a second AJAX call and some additional complication of the JavaScript.)
    # If a user later re-enters the verification / payment flow, she will create a new order.
    cart.start_purchase()

    callback_url = request.build_absolute_uri(
        reverse("shoppingcart.views.postpay_callback")
    )

    payment_data = {
        'payment_processor_name': settings.CC_PROCESSOR_NAME,
        'payment_page_url': get_purchase_endpoint(),
        'payment_form_data': get_signed_purchase_params(
            cart,
            callback_url=callback_url,
            extra_data=[six.text_type(course_key), course_mode.slug]
        ),
    }
    return payment_data


@require_POST
@login_required
def create_order(request):
    """
    This endpoint is named 'create_order' for backward compatibility, but its
    actual use is to add a single product to the user's cart and request
    immediate checkout.
    """
    course_id = request.POST['course_id']
    course_id = CourseKey.from_string(course_id)
    donation_for_course = request.session.get('donation_for_course', {})
    contribution = request.POST.get("contribution", donation_for_course.get(six.text_type(course_id), 0))
    try:
        amount = decimal.Decimal(contribution).quantize(decimal.Decimal('.01'), rounding=decimal.ROUND_DOWN)
    except decimal.InvalidOperation:
        return HttpResponseBadRequest(_("Selected price is not valid number."))

    current_mode = None
    sku = request.POST.get('sku', None)

    if sku:
        try:
            current_mode = CourseMode.objects.get(sku=sku)
        except CourseMode.DoesNotExist:
            log.exception(u'Failed to find CourseMode with SKU [%s].', sku)

    if not current_mode:
        # Check if there are more than 1 paid(mode with min_price>0 e.g verified/professional/no-id-professional) modes
        # for course exist then choose the first one
        paid_modes = CourseMode.paid_modes_for_course(course_id)
        if paid_modes:
            if len(paid_modes) > 1:
                log.warning(u"Multiple paid course modes found for course '%s' for create order request", course_id)
            current_mode = paid_modes[0]

    # Make sure this course has a paid mode
    if not current_mode:
        log.warning(u"Create order requested for course '%s' without a paid mode.", course_id)
        return HttpResponseBadRequest(_("This course doesn't support paid certificates"))

    if CourseMode.is_professional_mode(current_mode):
        amount = current_mode.min_price

    if amount < current_mode.min_price:
        return HttpResponseBadRequest(_("No selected price or selected price is below minimum."))

    if current_mode.sku:
        # if request.POST doesn't contain 'processor' then the service's default payment processor will be used.
        payment_data = checkout_with_ecommerce_service(
            request.user,
            course_id,
            current_mode,
            request.POST.get('processor')
        )
    else:
        payment_data = checkout_with_shoppingcart(request, request.user, course_id, current_mode, amount)

    if 'processor' not in request.POST:
        # (XCOM-214) To be removed after release.
        # the absence of this key in the POST payload indicates that the request was initiated from
        # a stale js client, which expects a response containing only the 'payment_form_data' part of
        # the payment data result.
        payment_data = payment_data['payment_form_data']
    return HttpResponse(json.dumps(payment_data), content_type="application/json")


class SubmitPhotosView(View):
    """
    End-point for submitting photos for verification.
    """

    @method_decorator(transaction.non_atomic_requests)
    def dispatch(self, request, *args, **kwargs):
        return super(SubmitPhotosView, self).dispatch(request, *args, **kwargs)

    @method_decorator(login_required)
    @method_decorator(outer_atomic(read_committed=True))
    def post(self, request):
        """
        Submit photos for verification.

        This end-point is used for the following cases:

        * Initial verification through the pay-and-verify flow.
        * Initial verification initiated from a checkpoint within a course.
        * Re-verification initiated from a checkpoint within a course.

        POST Parameters:

            face_image (str): base64-encoded image data of the user's face.
            photo_id_image (str): base64-encoded image data of the user's photo ID.
            full_name (str): The user's full name, if the user is requesting a name change as well.
            course_key (str): Identifier for the course, if initiated from a checkpoint.
            checkpoint (str): Location of the checkpoint in the course.

        """
        # If the user already has an initial verification attempt, we can re-use the photo ID
        # the user submitted with the initial attempt.
        initial_verification = SoftwareSecurePhotoVerification.get_initial_verification(request.user)

        # Validate the POST parameters
        params, response = self._validate_parameters(request, bool(initial_verification))
        if response is not None:
            return response

        # If necessary, update the user's full name
        if "full_name" in params:
            response = self._update_full_name(request.user, params["full_name"])
            if response is not None:
                return response

        # Retrieve the image data
        # Validation ensures that we'll have a face image, but we may not have
        # a photo ID image if this is a re-verification.
        face_image, photo_id_image, response = self._decode_image_data(
            params["face_image"], params.get("photo_id_image")
        )

        # If we have a photo_id we do not want use the initial verification image.
        if photo_id_image is not None:
            initial_verification = None

        if response is not None:
            return response

        # Submit the attempt
        self._submit_attempt(request.user, face_image, photo_id_image, initial_verification)

        self._fire_event(request.user, "edx.bi.verify.submitted", {"category": "verification"})
        self._send_confirmation_email(request.user)
        return JsonResponse({})

    def _validate_parameters(self, request, has_initial_verification):
        """
        Check that the POST parameters are valid.

        Arguments:
            request (HttpRequest): The request object.
            has_initial_verification (bool): Whether the user has an initial verification attempt.

        Returns:
            HttpResponse or None

        """
        # Pull out the parameters we care about.
        params = {
            param_name: request.POST[param_name]
            for param_name in [
                "face_image",
                "photo_id_image",
                "course_key",
                "full_name"
            ]
            if param_name in request.POST
        }

        # If the user already has an initial verification attempt, then we don't
        # require the user to submit a photo ID image, since we can re-use the photo ID
        # image from the initial attempt.
        # If we don't have an initial verification OR a photo ID image, something has gone
        # terribly wrong in the JavaScript.  Log this as an error so we can track it down.
        if "photo_id_image" not in params and not has_initial_verification:
            log.error(
                (
                    u"User %s does not have an initial verification attempt "
                    "and no photo ID image data was provided. "
                    "This most likely means that the JavaScript client is not "
                    "correctly constructing the request to submit photos."
                ), request.user.id
            )
            return None, HttpResponseBadRequest(
                _("Photo ID image is required if the user does not have an initial verification attempt.")
            )

        # The face image is always required.
        if "face_image" not in params:
            msg = _("Missing required parameter face_image")
            return None, HttpResponseBadRequest(msg)

        # If provided, parse the course key and checkpoint location
        if "course_key" in params:
            try:
                params["course_key"] = CourseKey.from_string(params["course_key"])
            except InvalidKeyError:
                return None, HttpResponseBadRequest(_("Invalid course key"))

        return params, None

    def _update_full_name(self, user, full_name):
        """
        Update the user's full name.

        Arguments:
            user (User): The user to update.
            full_name (unicode): The user's updated full name.

        Returns:
            HttpResponse or None

        """
        try:
            update_account_settings(user, {"name": full_name})
        except UserNotFound:
            return HttpResponseBadRequest(_("No profile found for user"))
        except AccountValidationError:
            msg = _(
                u"Name must be at least {min_length} character long."
            ).format(min_length=NAME_MIN_LENGTH)
            return HttpResponseBadRequest(msg)

    def _decode_image_data(self, face_data, photo_id_data=None):
        """
        Decode image data sent with the request.

        Arguments:
            face_data (str): base64-encoded face image data.

        Keyword Arguments:
            photo_id_data (str): base64-encoded photo ID image data.

        Returns:
            tuple of (str, str, HttpResponse)

        """
        try:
            # Decode face image data (used for both an initial and re-verification)
            face_image = decode_image_data(face_data)

            # Decode the photo ID image data if it's provided
            photo_id_image = (
                decode_image_data(photo_id_data)
                if photo_id_data is not None else None
            )

            return face_image, photo_id_image, None

        except InvalidImageData:
            msg = _("Image data is not valid.")
            return None, None, HttpResponseBadRequest(msg)

    def _submit_attempt(self, user, face_image, photo_id_image=None, initial_verification=None):
        """
        Submit a verification attempt.

        Arguments:
            user (User): The user making the attempt.
            face_image (str): Decoded face image data.

        Keyword Arguments:
            photo_id_image (str or None): Decoded photo ID image data.
            initial_verification (SoftwareSecurePhotoVerification): The initial verification attempt.
        """
        attempt = SoftwareSecurePhotoVerification(user=user)

        # We will always have face image data, so upload the face image
        attempt.upload_face_image(face_image)

        # If an ID photo wasn't submitted, re-use the ID photo from the initial attempt.
        # Earlier validation rules ensure that at least one of these is available.
        if photo_id_image is not None:
            attempt.upload_photo_id_image(photo_id_image)
        elif initial_verification is None:
            # Earlier validation should ensure that we never get here.
            log.error(
                "Neither a photo ID image or initial verification attempt provided. "
                "Parameter validation in the view should prevent this from happening!"
            )

        # Submit the attempt
        attempt.mark_ready()
        attempt.submit(copy_id_photo_from=initial_verification)

        return attempt

    def _send_confirmation_email(self, user):
        """
        Send an email confirming that the user submitted photos
        for initial verification.
        """
        context = {
            'full_name': user.profile.name,
            'platform_name': configuration_helpers.get_value("PLATFORM_NAME", settings.PLATFORM_NAME)
        }

        subject = _(u"{platform_name} ID Verification Photos Received").format(platform_name=context['platform_name'])
        message = render_to_string('emails/photo_submission_confirmation.txt', context)
        from_address = configuration_helpers.get_value('email_from_address', settings.DEFAULT_FROM_EMAIL)
        to_address = user.email

        try:
            send_mail(subject, message, from_address, [to_address], fail_silently=False)
        except:  # pylint: disable=bare-except
            # We catch all exceptions and log them.
            # It would be much, much worse to roll back the transaction due to an uncaught
            # exception than to skip sending the notification email.
            log.exception(u"Could not send notification email for initial verification for user %s", user.id)

    def _fire_event(self, user, event_name, parameters):
        """
        Fire an analytics event.

        Arguments:
            user (User): The user who submitted photos.
            event_name (str): Name of the analytics event.
            parameters (dict): Event parameters.

        Returns: None

        """
        segment.track(user.id, event_name, parameters)


def _compose_message_reverification_email(
        course_key, user_id, related_assessment_location, status, request
):  # pylint: disable=invalid-name
    """
    Compose subject and message for photo reverification email.

    Args:
        course_key(CourseKey): CourseKey object
        user_id(str): User Id
        related_assessment_location(str): Location of reverification XBlock
        photo_verification(QuerySet): Queryset of SoftwareSecure objects
        status(str): Approval status
        is_secure(Bool): Is running on secure protocol or not

    Returns:
        None if any error occurred else Tuple of subject and message strings
    """
    try:
        usage_key = UsageKey.from_string(related_assessment_location)
        reverification_block = modulestore().get_item(usage_key)

        course = modulestore().get_course(course_key)
        redirect_url = get_redirect_url(course_key, usage_key.replace(course_key=course_key))

        subject = "Re-verification Status"
        context = {
            "status": status,
            "course_name": course.display_name_with_default_escaped,
            "assessment": reverification_block.related_assessment
        }

        # Allowed attempts is 1 if not set on verification block
        allowed_attempts = reverification_block.attempts + 1
        used_attempts = VerificationStatus.get_user_attempts(user_id, course_key, related_assessment_location)
        left_attempts = allowed_attempts - used_attempts
        is_attempt_allowed = left_attempts > 0
        verification_open = True
        if reverification_block.due:
            verification_open = timezone.now() <= reverification_block.due

        context["left_attempts"] = left_attempts
        context["is_attempt_allowed"] = is_attempt_allowed
        context["verification_open"] = verification_open
        context["due_date"] = get_default_time_display(reverification_block.due)

        context['platform_name'] = configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME)
        context["used_attempts"] = used_attempts
        context["allowed_attempts"] = allowed_attempts
        context["support_link"] = configuration_helpers.get_value('email_from_address', settings.CONTACT_EMAIL)

        re_verification_link = reverse(
            'verify_student_incourse_reverify',
            args=(
                str(course_key),
                related_assessment_location
            )
        )

        context["course_link"] = request.build_absolute_uri(redirect_url)
        context["reverify_link"] = request.build_absolute_uri(re_verification_link)

        message = render_to_string('emails/reverification_processed.txt', context)
        log.info(
            "Sending email to User_Id=%s. Attempts left for this user are %s. "
            "Allowed attempts %s. "
            "Due Date %s",
            str(user_id), left_attempts, allowed_attempts, str(reverification_block.due)
        )
        return subject, message
    # Catch all exception to avoid raising back to view
    except:  # pylint: disable=bare-except
        log.exception("The email for re-verification sending failed for user_id %s", user_id)


def _send_email(user_id, subject, message):
    """ Send email to given user

    Args:
        user_id(str): User Id
        subject(str): Subject lines of emails
        message(str): Email message body

    Returns:
        None
    """
    from_address = configuration_helpers.get_value(
        'email_from_address',
        settings.DEFAULT_FROM_EMAIL
    )
    user = User.objects.get(id=user_id)
    user.email_user(subject, message, from_address)


def _set_user_requirement_status(attempt, namespace, status, reason=None):
    """Sets the status of a credit requirement for the user,
    based on a verification checkpoint.
    """
    checkpoint = None
    try:
        checkpoint = VerificationCheckpoint.objects.get(photo_verification=attempt)
    except VerificationCheckpoint.DoesNotExist:
        log.error("Unable to find checkpoint for user with id %d", attempt.user.id)

    if checkpoint is not None:
        try:
            set_credit_requirement_status(
                attempt.user.username,
                checkpoint.course_id,
                namespace,
                checkpoint.checkpoint_location,
                status=status,
                reason=reason,
            )
        except Exception:  # pylint: disable=broad-except
            # Catch exception if unable to add credit requirement
            # status for user
            log.error("Unable to add Credit requirement status for user with id %d", attempt.user.id)


@require_POST
@csrf_exempt  # SS does its own message signing, and their API won't have a cookie value
def results_callback(request):
    """
    Software Secure will call this callback to tell us whether a user is
    verified to be who they said they are.
    """
    body = request.body

    try:
        body_dict = json.loads(body.decode('utf-8'))
    except ValueError:
        log.exception(u"Invalid JSON received from Software Secure:\n\n{}\n".format(body))
        return HttpResponseBadRequest(u"Invalid JSON. Received:\n\n{}".format(body))

    if not isinstance(body_dict, dict):
        log.error(u"Reply from Software Secure is not a dict:\n\n{}\n".format(body))
        return HttpResponseBadRequest(u"JSON should be dict. Received:\n\n{}".format(body))

    headers = {
        "Authorization": request.META.get("HTTP_AUTHORIZATION", ""),
        "Date": request.META.get("HTTP_DATE", "")
    }

    has_valid_signature(
        "POST",
        headers,
        body_dict,
        settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_ACCESS_KEY"],
        settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_SECRET_KEY"]
    )

    _response, access_key_and_sig = headers["Authorization"].split(" ")
    access_key = access_key_and_sig.split(":")[0]

    # This is what we should be doing...
    # if not sig_valid:
    #    return HttpResponseBadRequest("Signature is invalid")

    # This is what we're doing until we can figure out why we disagree on sigs
    if access_key != settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_ACCESS_KEY"]:
        return HttpResponseBadRequest("Access key invalid")

    receipt_id = body_dict.get("EdX-ID")
    result = body_dict.get("Result")
    reason = body_dict.get("Reason", "")
    error_code = body_dict.get("MessageType", "")

    try:
        attempt = SoftwareSecurePhotoVerification.objects.get(receipt_id=receipt_id)
    except SoftwareSecurePhotoVerification.DoesNotExist:
        log.error(u"Software Secure posted back for receipt_id %s, but not found", receipt_id)
        return HttpResponseBadRequest(u"edX ID {} not found".format(receipt_id))

    user = attempt.user
    verification_status_email_vars = {
        'platform_name': settings.PLATFORM_NAME,
    }
    if result == "PASS":
        # If this verification is not an outdated version then make expiry date of previous approved verification NULL
        # Setting expiry date to NULL is important so that it does not get filtered in the management command
        # that sends email when verification expires : verify_student/send_verification_expiry_email
        if attempt.status != 'approved':
            verification = SoftwareSecurePhotoVerification.objects.filter(status='approved', user_id=attempt.user_id)
            if verification:
                log.info(u'Making expiry date of previous approved verification NULL for {}'.format(attempt.user_id))
                # The updated_at field in sspv model has auto_now set to True, which means any time save() is called on
                # the model instance, `updated_at` will change. Some of the existing functionality of verification
                # (showing your verification has expired on dashboard) relies on updated_at.
                # In case the attempt.approve() fails for some reason and to not cause any inconsistencies in existing
                # functionality update() is called instead of save()
                previous_verification = verification.latest('updated_at')
                SoftwareSecurePhotoVerification.objects.filter(pk=previous_verification.pk
                                                               ).update(expiry_date=None, expiry_email_date=None)
        log.debug(u'Approving verification for {}'.format(receipt_id))
        attempt.approve()
        status = u"approved"
        expiry_date = datetime.date.today() + datetime.timedelta(
            days=settings.VERIFY_STUDENT["DAYS_GOOD_FOR"]
        )
        verification_status_email_vars['expiry_date'] = expiry_date.strftime("%m/%d/%Y")
        verification_status_email_vars['full_name'] = user.profile.name
        subject = _(u"Your {platform_name} ID Verification Approved").format(
            platform_name=settings.PLATFORM_NAME
        )
        context = {
            'subject': subject,
            'template': 'emails/passed_verification_email.txt',
            'email': user.email,
            'email_vars': verification_status_email_vars
        }
        send_verification_status_email.delay(context)

    elif result == "FAIL":
        log.debug(u"Denying verification for %s", receipt_id)
        attempt.deny(json.dumps(reason), error_code=error_code)
        status = "denied"
        reverify_url = '{}{}'.format(settings.LMS_ROOT_URL, reverse("verify_student_reverify"))
        verification_status_email_vars['reasons'] = reason
        verification_status_email_vars['reverify_url'] = reverify_url
        verification_status_email_vars['faq_url'] = settings.ID_VERIFICATION_SUPPORT_LINK
        subject = _(u"Your {platform_name} Verification Has Been Denied").format(
            platform_name=settings.PLATFORM_NAME
        )
        context = {
            'subject': subject,
            'template': 'emails/failed_verification_email.txt',
            'email': user.email,
            'email_vars': verification_status_email_vars
        }
        send_verification_status_email.delay(context)

    elif result == "SYSTEM FAIL":
        log.debug(u"System failure for %s -- resetting to must_retry", receipt_id)
        attempt.system_error(json.dumps(reason), error_code=error_code)
        status = "error"
        log.error(u"Software Secure callback attempt for %s failed: %s", receipt_id, reason)
    else:
        log.error(u"Software Secure returned unknown result %s", result)
        return HttpResponseBadRequest(
            u"Result {} not understood. Known results: PASS, FAIL, SYSTEM FAIL".format(result)
        )

    return HttpResponse("OK!")


class VerificationStatusAPIView(APIView):
    """
    GET /verify_student/status/

    Parameters: None

    Returns:
        200 OK
        {
            "status": String,
            "expires": String,
            "can_verify": Boolean
        }

    Notes:
        * "status" is a verification status string, or "none" if there is none.
        * Verification should be allowed if and only if "can_verify" is true.
        * If there is a current verification, then "expires" is a ISO datetime string.
        * Otherwise, "expires" is omitted.
    """
    @method_decorator(login_required)
    def get(self, request):
        """
        Handle the GET request.
        """
        verification_status = IDVerificationService.user_status(request.user)
        expiration_datetime = IDVerificationService.get_expiration_datetime(request.user, ['approved'])
        can_verify = can_verify_now(verification_status, expiration_datetime)
        data = {
            'status': verification_status['status'],
            'can_verify': can_verify,
        }
        if expiration_datetime:
            data['expires'] = expiration_datetime
        return Response(data)


class ReverifyView(View):
    """
    Reverification occurs when a user's initial verification is denied
    or expires.  When this happens, users can re-submit photos through
    the re-verification flow.

    Unlike in-course reverification, this flow requires users to submit
    *both* face and ID photos.  In contrast, during in-course reverification,
    students submit only face photos, which are matched against the ID photo
    the user submitted during initial verification.

    """

    @method_decorator(login_required)
    def get(self, request):
        """
        Render the reverification flow.

        Most of the work is done client-side by composing the same
        Backbone views used in the initial verification flow.
        """
        verification_status = IDVerificationService.user_status(request.user)
        expiration_datetime = IDVerificationService.get_expiration_datetime(request.user, ['approved'])
        if can_verify_now(verification_status, expiration_datetime):
            context = {
                "user_full_name": request.user.profile.name,
                "platform_name": configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME),
                "capture_sound": staticfiles_storage.url("audio/camera_capture.wav"),
            }
            return render_to_response("verify_student/reverify.html", context)
        else:
            context = {
                "status": verification_status['status']
            }
            return render_to_response("verify_student/reverify_not_allowed.html", context)


class InCourseReverifyView(View):
    """
    The in-course reverification view.

    In-course reverification occurs while a student is taking a course.
    At points in the course, students are prompted to submit face photos,
    which are matched against the ID photos the user submitted during their
    initial verification.

    Students are prompted to enter this flow from an "In Course Reverification"
    XBlock (courseware component) that course authors add to the course.
    See https://github.com/edx/edx-reverification-block for more details.

    """
    @method_decorator(login_required)
    def get(self, request, course_id, usage_id):
        """Display the view for face photo submission.

        Args:
            request(HttpRequest): HttpRequest object
            course_id(str): A string of course id
            usage_id(str): Location of Reverification XBlock in courseware

        Returns:
            HttpResponse
        """
        user = request.user
        course_key = CourseKey.from_string(course_id)
        course = modulestore().get_course(course_key)
        if course is None:
            log.error(u"Could not find course '%s' for in-course reverification.", course_key)
            raise Http404

        try:
            checkpoint = VerificationCheckpoint.objects.get(course_id=course_key, checkpoint_location=usage_id)
        except VerificationCheckpoint.DoesNotExist:
            log.error(
                u"No verification checkpoint exists for the "
                u"course '%s' and checkpoint location '%s'.",
                course_key, usage_id
            )
            raise Http404

        initial_verification = SoftwareSecurePhotoVerification.get_initial_verification(user)
        if not initial_verification:
            return self._redirect_to_initial_verification(user, course_key, usage_id)

        # emit the reverification event
        self._track_reverification_events('edx.bi.reverify.started', user.id, course_id, checkpoint.checkpoint_name)

        context = {
            'course_key': str(course_key),
            'course_name': course.display_name_with_default_escaped,
            'checkpoint_name': checkpoint.checkpoint_name,
            'platform_name': configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME),
            'usage_id': usage_id,
            'capture_sound': staticfiles_storage.url("audio/camera_capture.wav"),
        }
        return render_to_response("verify_student/incourse_reverify.html", context)

    def _track_reverification_events(self, event_name, user_id, course_id, checkpoint):
        """Track re-verification events for a user against a reverification
        checkpoint of a course.

        Arguments:
            event_name (str): Name of event being tracked
            user_id (str): The ID of the user
            course_id (unicode): ID associated with the course
            checkpoint (str): Checkpoint name

        Returns:
            None
        """
        log.info(
            u"In-course reverification: event %s occurred for user '%s' in course '%s' at checkpoint '%s'",
            event_name, user_id, course_id, checkpoint
        )

        if settings.LMS_SEGMENT_KEY:
            tracking_context = tracker.get_tracker().resolve_context()
            analytics.track(
                user_id,
                event_name,
                {
                    'category': "verification",
                    'label': str(course_id),
                    'checkpoint': checkpoint
                },
                context={
                    'ip': tracking_context.get('ip'),
                    'Google Analytics': {
                        'clientId': tracking_context.get('client_id')
                    }
                }
            )

    def _redirect_to_initial_verification(self, user, course_key, checkpoint):
        """
        Redirect because the user does not have an initial verification.

        We will redirect the user to the initial verification flow,
        passing the identifier for this checkpoint.  When the user
        submits a verification attempt, it will count for *both*
        the initial and checkpoint verification.

        Arguments:
            user (User): The user who made the request.
            course_key (CourseKey): The identifier for the course for which
                the user is attempting to re-verify.
            checkpoint (string): Location of the checkpoint in the courseware.

        Returns:
            HttpResponse

        """
        log.info(
            u"User %s does not have an initial verification, so "
            u"he/she will be redirected to the \"verify later\" flow "
            u"for the course %s.",
            user.id, course_key
        )
        base_url = reverse('verify_student_verify_now', kwargs={'course_id': str(course_key)})
        params = urllib.urlencode({"checkpoint": checkpoint})
        full_url = u"{base}?{params}".format(base=base_url, params=params)
        return redirect(full_url)
