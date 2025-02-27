"""
Unit tests for contentstore.views.library

More important high-level tests are in contentstore/tests/test_libraries.py
"""


import ddt
import mock
from django.conf import settings
from mock import patch
from opaque_keys.edx.locator import CourseKey, LibraryLocator
from six import binary_type, text_type
from six.moves import range

from contentstore.tests.utils import AjaxEnabledTestClient, CourseTestCase, parse_json
from contentstore.utils import reverse_course_url, reverse_library_url
from contentstore.views.component import get_component_templates
from contentstore.views.library import get_library_creator_status
from cms.djangoapps.course_creators.views import add_user_with_status_granted as grant_course_creator_status
from student.roles import LibraryUserRole
from xmodule.modulestore.tests.factories import LibraryFactory

LIBRARY_REST_URL = '/library/'  # URL for GET/POST requests involving libraries


def make_url_for_lib(key):
    """ Get the RESTful/studio URL for testing the given library """
    if isinstance(key, LibraryLocator):
        key = text_type(key)
    return LIBRARY_REST_URL + key


@ddt.ddt
@mock.patch.dict('django.conf.settings.FEATURES', {'DISABLE_COURSE_CREATION': False})
class UnitTestLibraries(CourseTestCase):
    """
    Unit tests for library views
    """

    def setUp(self):
        super(UnitTestLibraries, self).setUp()

        self.client = AjaxEnabledTestClient()
        self.client.login(username=self.user.username, password=self.user_password)

    ######################################################
    # Tests for /library/ - list and create libraries:

    @mock.patch("contentstore.views.library.LIBRARIES_ENABLED", False)
    def test_library_creator_status_libraries_not_enabled(self):
        _, nostaff_user = self.create_non_staff_authed_user_client()
        self.assertEqual(get_library_creator_status(nostaff_user), False)

    @mock.patch("contentstore.views.library.LIBRARIES_ENABLED", True)
    def test_library_creator_status_with_is_staff_user(self):
        self.assertEqual(get_library_creator_status(self.user), True)

    @mock.patch("contentstore.views.library.LIBRARIES_ENABLED", True)
    def test_library_creator_status_with_course_creator_role(self):
        _, nostaff_user = self.create_non_staff_authed_user_client()
        with mock.patch.dict('django.conf.settings.FEATURES', {"ENABLE_CREATOR_GROUP": True}):
            grant_course_creator_status(self.user, nostaff_user)
            self.assertEqual(get_library_creator_status(nostaff_user), True)

    @mock.patch("contentstore.views.library.LIBRARIES_ENABLED", True)
    def test_library_creator_status_with_no_course_creator_role(self):
        _, nostaff_user = self.create_non_staff_authed_user_client()
        self.assertEqual(get_library_creator_status(nostaff_user), True)

    @ddt.data(
        (False, False, True),
        (False, True, False),
        (True, False, True),
        (True, True, False),
        (True, None, False),
        (False, None, True)
    )
    @ddt.unpack
    def test_library_creator_status_settings(self, disable_course, disable_library, expected_status):
        """
        Ensure that the setting DISABLE_LIBRARY_CREATION overrides DISABLE_COURSE_CREATION as expected.
        """
        _, nostaff_user = self.create_non_staff_authed_user_client()
        with mock.patch("contentstore.views.library.LIBRARIES_ENABLED", True):
            with mock.patch.dict(
                "django.conf.settings.FEATURES",
                {
                    "DISABLE_COURSE_CREATION": disable_course,
                    "DISABLE_LIBRARY_CREATION": disable_library
                }
            ):
                self.assertEqual(get_library_creator_status(nostaff_user), expected_status)

    @mock.patch.dict('django.conf.settings.FEATURES', {'DISABLE_COURSE_CREATION': True})
    @mock.patch("contentstore.views.library.LIBRARIES_ENABLED", True)
    def test_library_creator_status_with_no_course_creator_role_and_disabled_nonstaff_course_creation(self):
        """
        Ensure that `DISABLE_COURSE_CREATION` feature works with libraries as well.
        """
        nostaff_client, nostaff_user = self.create_non_staff_authed_user_client()
        self.assertFalse(get_library_creator_status(nostaff_user))

        # To be explicit, this user can GET, but not POST
        get_response = nostaff_client.get_json(LIBRARY_REST_URL)
        post_response = nostaff_client.ajax_post(LIBRARY_REST_URL, {
            'org': 'org', 'library': 'lib', 'display_name': "New Library",
        })
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 403)

    @patch("contentstore.views.library.LIBRARIES_ENABLED", False)
    def test_with_libraries_disabled(self):
        """
        The library URLs should return 404 if libraries are disabled.
        """
        response = self.client.get_json(LIBRARY_REST_URL)
        self.assertEqual(response.status_code, 404)

    def test_list_libraries(self):
        """
        Test that we can GET /library/ to list all libraries visible to the current user.
        """
        # Create some more libraries
        libraries = [LibraryFactory.create() for _ in range(3)]
        lib_dict = dict([(lib.location.library_key, lib) for lib in libraries])

        response = self.client.get_json(LIBRARY_REST_URL)
        self.assertEqual(response.status_code, 200)
        lib_list = parse_json(response)
        self.assertEqual(len(lib_list), len(libraries))
        for entry in lib_list:
            self.assertIn("library_key", entry)
            self.assertIn("display_name", entry)
            key = CourseKey.from_string(entry["library_key"])
            self.assertIn(key, lib_dict)
            self.assertEqual(entry["display_name"], lib_dict[key].display_name)
            del lib_dict[key]  # To ensure no duplicates are matched

    @ddt.data("delete", "put")
    def test_bad_http_verb(self, verb):
        """
        We should get an error if we do weird requests to /library/
        """
        response = getattr(self.client, verb)(LIBRARY_REST_URL)
        self.assertEqual(response.status_code, 405)

    def test_create_library(self):
        """ Create a library. """
        response = self.client.ajax_post(LIBRARY_REST_URL, {
            'org': 'org',
            'library': 'lib',
            'display_name': "New Library",
        })
        self.assertEqual(response.status_code, 200)
        # That's all we check. More detailed tests are in contentstore.tests.test_libraries...

    @patch.dict('django.conf.settings.FEATURES', {'ENABLE_CREATOR_GROUP': True})
    def test_lib_create_permission(self):
        """
        Users who are given course creator roles should be able to create libraries.
        """
        self.client.logout()
        ns_user, password = self.create_non_staff_user()
        self.client.login(username=ns_user.username, password=password)
        grant_course_creator_status(self.user, ns_user)
        response = self.client.ajax_post(LIBRARY_REST_URL, {
            'org': 'org', 'library': 'lib', 'display_name': "New Library",
        })
        self.assertEqual(response.status_code, 200)

    @patch.dict('django.conf.settings.FEATURES', {'ENABLE_CREATOR_GROUP': False})
    def test_lib_create_permission_no_course_creator_role_and_no_course_creator_group(self):
        """
        Users who are not given course creator roles should still be able to create libraries
        if ENABLE_CREATOR_GROUP is not enabled.
        """
        self.client.logout()
        ns_user, password = self.create_non_staff_user()
        self.client.login(username=ns_user.username, password=password)
        response = self.client.ajax_post(LIBRARY_REST_URL, {
            'org': 'org', 'library': 'lib', 'display_name': "New Library",
        })
        self.assertEqual(response.status_code, 200)

    @patch.dict('django.conf.settings.FEATURES', {'ENABLE_CREATOR_GROUP': True})
    def test_lib_create_permission_no_course_creator_role_and_course_creator_group(self):
        """
        Users who are not given course creator roles should not be able to create libraries
        if ENABLE_CREATOR_GROUP is enabled.
        """
        self.client.logout()
        ns_user, password = self.create_non_staff_user()
        self.client.login(username=ns_user.username, password=password)
        response = self.client.ajax_post(LIBRARY_REST_URL, {
            'org': 'org', 'library': 'lib', 'display_name': "New Library",
        })
        self.assertEqual(response.status_code, 403)

    @ddt.data(
        {},
        {'org': 'org'},
        {'library': 'lib'},
        {'org': 'C++', 'library': 'lib', 'display_name': 'Lib with invalid characters in key'},
        {'org': 'Org', 'library': 'Wh@t?', 'display_name': 'Lib with invalid characters in key'},
    )
    def test_create_library_invalid(self, data):
        """
        Make sure we are prevented from creating libraries with invalid keys/data
        """
        response = self.client.ajax_post(LIBRARY_REST_URL, data)
        self.assertEqual(response.status_code, 400)

    def test_no_duplicate_libraries(self):
        """
        We should not be able to create multiple libraries with the same key
        """
        lib = LibraryFactory.create()
        lib_key = lib.location.library_key
        response = self.client.ajax_post(LIBRARY_REST_URL, {
            'org': lib_key.org,
            'library': lib_key.library,
            'display_name': "A Duplicate key, same as 'lib'",
        })
        self.assertIn('already a library defined', parse_json(response)['ErrMsg'])
        self.assertEqual(response.status_code, 400)

    ######################################################
    # Tests for /library/:lib_key/ - get a specific library as JSON or HTML editing view

    def test_get_lib_info(self):
        """
        Test that we can get data about a library (in JSON format) using /library/:key/
        """
        # Create a library
        lib_key = LibraryFactory.create().location.library_key
        # Re-load the library from the modulestore, explicitly including version information:
        lib = self.store.get_library(lib_key, remove_version=False, remove_branch=False)
        version = lib.location.library_key.version_guid
        self.assertNotEqual(version, None)

        response = self.client.get_json(make_url_for_lib(lib_key))
        self.assertEqual(response.status_code, 200)
        info = parse_json(response)
        self.assertEqual(info['display_name'], lib.display_name)
        self.assertEqual(info['library_id'], text_type(lib_key))
        self.assertEqual(info['previous_version'], None)
        self.assertNotEqual(info['version'], None)
        self.assertNotEqual(info['version'], '')
        self.assertEqual(info['version'], text_type(version))

    def test_get_lib_edit_html(self):
        """
        Test that we can get the studio view for editing a library using /library/:key/
        """
        lib = LibraryFactory.create()

        response = self.client.get(make_url_for_lib(lib.location.library_key))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<html")
        self.assertContains(response, lib.display_name)

    @ddt.data('library-v1:Nonexistent+library', 'course-v1:Org+Course', 'course-v1:Org+Course+Run', 'invalid')
    def test_invalid_keys(self, key_str):
        """
        Check that various Nonexistent/invalid keys give 404 errors
        """
        response = self.client.get_json(make_url_for_lib(key_str))
        self.assertEqual(response.status_code, 404)

    def test_bad_http_verb_with_lib_key(self):
        """
        We should get an error if we do weird requests to /library/
        """
        lib = LibraryFactory.create()
        for verb in ("post", "delete", "put"):
            response = getattr(self.client, verb)(make_url_for_lib(lib.location.library_key))
            self.assertEqual(response.status_code, 405)

    def test_no_access(self):
        user, password = self.create_non_staff_user()
        self.client.login(username=user, password=password)

        lib = LibraryFactory.create()
        response = self.client.get(make_url_for_lib(lib.location.library_key))
        self.assertEqual(response.status_code, 403)

    def test_get_component_templates(self):
        """
        Verify that templates for adding discussion and advanced components to
        content libraries are not provided.
        """
        lib = LibraryFactory.create()
        lib.advanced_modules = ['lti']
        lib.save()
        templates = [template['type'] for template in get_component_templates(lib, library=True)]
        self.assertIn('problem', templates)
        self.assertNotIn('discussion', templates)
        self.assertNotIn('advanced', templates)

    def test_advanced_problem_types(self):
        """
        Verify that advanced problem types are not provided in problem component for libraries.
        """
        lib = LibraryFactory.create()
        lib.save()

        problem_type_templates = next(
            (component['templates'] for component in get_component_templates(lib, library=True) if component['type'] == 'problem'),
            []
        )
        # Each problem template has a category which shows whether problem is a 'problem'
        # or which of the advanced problem type (e.g drag-and-drop-v2).
        problem_type_categories = [problem_template['category'] for problem_template in problem_type_templates]

        for advance_problem_type in settings.ADVANCED_PROBLEM_TYPES:
            self.assertNotIn(advance_problem_type['component'], problem_type_categories)

    def test_manage_library_users(self):
        """
        Simple test that the Library "User Access" view works.
        Also tests that we can use the REST API to assign a user to a library.
        """
        library = LibraryFactory.create()
        extra_user, _ = self.create_non_staff_user()
        manage_users_url = reverse_library_url('manage_library_users', text_type(library.location.library_key))

        response = self.client.get(manage_users_url)
        self.assertEqual(response.status_code, 200)
        # extra_user has not been assigned to the library so should not show up in the list:
        self.assertNotContains(response, extra_user.username)

        # Now add extra_user to the library:
        user_details_url = reverse_course_url(
            'course_team_handler',
            library.location.library_key, kwargs={'email': extra_user.email}
        )
        edit_response = self.client.ajax_post(user_details_url, {"role": LibraryUserRole.ROLE})
        self.assertIn(edit_response.status_code, (200, 204))

        # Now extra_user should apear in the list:
        response = self.client.get(manage_users_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, extra_user.username)
