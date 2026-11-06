import base64
import errno
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gazevoice.jobs import JobError
from gazevoice.paths import AppPaths
from gazevoice.voice.brains import claude_session_argv
from gazevoice.voice.leftovers import (
    CHECK_QUESTION,
    Place,
    find_leftovers,
    leftover_roots,
    make_noise_picture,
    run_leftover_check,
    search,
)
from gazevoice.voice.picture import PICTURE_MODEL, PICTURE_RULES, pictures_allowed, write_picture_check

from .helpers import make_job
from .test_picture import FAKE_CLAUDE, LOGGED_OUT, DescribeCropBase, claude_runner, gone, tree

NOT_ROOT = unittest.skipIf(os.geteuid() == 0, "root can read a file nobody can")


class FindLeftoversTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.scan = self.root / "scan"
        self.scan.mkdir()
        self.marker = os.urandom(64)
        self.since = time.time() - 60

    def tearDown(self):
        self.tmp.cleanup()

    def plant(self, name, where=None, before=b"junk", after=b"more junk", marker=None):
        path = (where or self.scan) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(before + (self.marker if marker is None else marker) + after)
        return path

    def test_find_leftovers_finds_a_planted_copy(self):
        copy = self.plant("deep/er/session.log")
        self.plant("other.txt", marker=b"something else")
        self.assertEqual(find_leftovers(self.marker, self.since, [self.scan]), [copy])
        self.assertEqual(find_leftovers(self.marker, self.since, [self.root / "empty"]), [])

    def test_a_copy_across_a_read_boundary_is_found(self):
        big = self.scan / "big.bin"
        chunk = 1 << 20
        big.write_bytes(b"\0" * (chunk - 30) + self.marker + b"\0" * chunk)  # the marker straddles the first 1 MiB
        self.assertEqual(find_leftovers(self.marker, self.since, [self.scan]), [big])

    def test_a_file_root_works_and_a_copy_is_listed_once(self):
        copy = self.plant("claude.json")
        self.assertEqual(find_leftovers(self.marker, self.since, [copy]), [copy])
        self.assertEqual(find_leftovers(self.marker, self.since, [self.scan, self.scan, copy]), [copy])

    def test_a_file_untouched_since_the_run_began_is_not_even_opened(self):
        old = self.plant("old.log")
        time.sleep(1.1)  # a file's change time can't be set back: it has to really be older
        since = time.time()
        fresh = self.plant("fresh.log")
        opened = []
        real_open = os.open

        def spying_open(path, *args, **kwargs):
            opened.append(Path(path))
            return real_open(path, *args, **kwargs)

        with patch("gazevoice.voice.leftovers.os.open", spying_open):
            found = find_leftovers(self.marker, since, [self.scan])
        self.assertEqual(found, [fresh])
        self.assertNotIn(old, opened)

    def test_a_file_whose_modified_time_was_set_back_but_which_changed_just_now_is_still_looked_at(self):
        sneaky = self.plant("sneaky.log")
        os.utime(sneaky, (self.since - 1000, self.since - 1000))  # old modified time; the change time is now
        self.assertEqual(find_leftovers(self.marker, self.since, [self.scan]), [sneaky])

    def test_a_file_shorter_than_the_marker_is_not_opened(self):
        tiny = self.scan / "tiny.txt"
        tiny.write_bytes(self.marker[:10])
        with patch("gazevoice.voice.leftovers.os.open", side_effect=AssertionError("opened")):
            self.assertEqual(find_leftovers(self.marker, self.since, [self.scan]), [])

    def test_symlinks_are_not_followed_out_of_the_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        elsewhere = self.plant("secret.txt", where=outside)
        (self.scan / "link.txt").symlink_to(elsewhere)
        (self.scan / "linked-folder").symlink_to(outside, target_is_directory=True)
        self.assertEqual(find_leftovers(self.marker, self.since, [self.scan]), [])
        inside = self.plant("real.txt")
        self.assertEqual(find_leftovers(self.marker, self.since, [self.scan]), [inside])

    def test_things_that_are_not_files_are_skipped_without_hanging(self):
        os.mkfifo(self.scan / "pipe")
        (self.scan / "folder.txt").mkdir()
        scan = search([self.marker], self.since, [Place(self.scan, strict=True)])
        self.assertEqual((scan.found, scan.failed, scan.skipped), ([], {}, set()))


class SearchTroubleTest(unittest.TestCase):
    """What the search couldn't look at is not the same as nothing found."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.scan = self.root / "scan"
        self.scan.mkdir()
        self.marker = os.urandom(64)
        self.since = time.time() - 60

    def tearDown(self):
        for path in self.root.rglob("*"):  # so that the folder can be removed
            path.chmod(0o700)
        self.tmp.cleanup()

    def locked_file(self, where=None, name="locked.bin"):
        path = (where or self.scan) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"junk" + self.marker)
        path.chmod(0)
        return path

    def search(self, *places):
        return search([self.marker], self.since, list(places))

    @NOT_ROOT
    def test_an_unreadable_file_of_mine_in_a_strict_place_means_the_search_cant_be_trusted(self):
        locked = self.locked_file()
        scan = self.search(Place(self.scan, strict=True))
        self.assertEqual(list(scan.failed), [str(locked)])
        self.assertEqual((scan.found, scan.skipped), ([], set()))

    @NOT_ROOT
    def test_in_a_wide_place_it_is_only_counted(self):
        locked = self.locked_file()
        scan = self.search(Place(self.scan))
        self.assertEqual((scan.failed, scan.skipped, scan.found), ({}, {str(locked)}, []))
        plain = self.search(self.scan)  # a bare path is a wide place
        self.assertEqual((plain.failed, plain.skipped), ({}, {str(locked)}))

    @NOT_ROOT
    def test_other_users_files_are_ignored_even_in_a_strict_place(self):
        locked = self.locked_file()
        with patch("gazevoice.voice.leftovers.os.getuid", return_value=os.getuid() + 1):
            scan = self.search(Place(self.scan, strict=True))
        self.assertEqual((scan.failed, scan.skipped), ({}, {str(locked)}))

    @NOT_ROOT
    def test_an_unreadable_folder_that_changed_since_could_hold_a_new_file_so_it_fails_a_strict_place(self):
        shut = self.scan / "shut"
        shut.mkdir()
        (shut / "inside.bin").write_bytes(self.marker)
        shut.chmod(0)
        strict = self.search(Place(self.scan, strict=True))
        self.assertEqual(list(strict.failed), [str(shut)])
        wide = self.search(Place(self.scan))
        self.assertEqual((wide.failed, wide.skipped), ({}, {str(shut)}))

    @NOT_ROOT
    def test_an_unreadable_folder_that_hasnt_changed_since_is_only_counted_as_macos_protects_some_in_the_temp_folder(self):
        shut = self.scan / "com.apple.something" / "TemporaryItems"
        shut.mkdir(parents=True)
        shut.chmod(0)
        time.sleep(1.1)  # the folder's change time is now behind the moment the picture is sent
        scan = search([self.marker], time.time(), [Place(self.scan, strict=True)])
        self.assertEqual((scan.failed, scan.skipped), ({}, {str(shut)}))

    def test_a_folder_macos_itself_refuses_to_list_is_only_counted_even_if_it_changed_just_now(self):
        protected = self.scan / "com.apple.thing" / "TemporaryItems"
        protected.mkdir(parents=True)
        real_walk = os.walk

        def walk(top, onerror=None, **kwargs):
            onerror(PermissionError(errno.EPERM, "Operation not permitted", str(protected)))
            yield from real_walk(top, **kwargs)

        with patch("gazevoice.voice.leftovers.os.walk", walk):
            scan = self.search(Place(self.scan, strict=True))
        self.assertEqual((scan.failed, scan.skipped), ({}, {str(protected)}))

    def test_a_file_macos_refuses_to_open_that_changed_since_is_a_failure(self):
        self.locked_file().chmod(0o600)
        with patch("gazevoice.voice.leftovers.os.open", side_effect=PermissionError(errno.EPERM, "Operation not permitted")):
            scan = self.search(Place(self.scan, strict=True))
        self.assertEqual(list(scan.failed), [str(self.scan / "locked.bin")])

    @NOT_ROOT
    def test_a_strict_place_that_cant_itself_be_listed_is_a_failure_however_old_it_is(self):
        time.sleep(1.1)
        self.scan.chmod(0)
        scan = search([self.marker], time.time(), [Place(self.scan, strict=True)])
        self.assertEqual(list(scan.failed), [str(self.scan)])

    @NOT_ROOT
    def test_a_strict_place_inside_a_wide_one_is_judged_strictly_whichever_comes_first(self):
        locked = self.locked_file(self.scan / "inner")
        for places in ([Place(self.scan), Place(self.scan / "inner", strict=True)],
                       [Place(self.scan / "inner", strict=True), Place(self.scan)]):
            scan = self.search(*places)
            self.assertEqual(list(scan.failed), [str(locked)])
            self.assertEqual(scan.skipped, set())

    def test_a_file_that_vanishes_while_it_is_looked_at_is_no_trouble(self):
        self.locked_file().chmod(0o600)
        for error in (FileNotFoundError(2, "No such file or directory"), OSError(62, "Too many levels of symbolic links")):
            with patch("gazevoice.voice.leftovers.os.open", side_effect=error):
                scan = self.search(Place(self.scan, strict=True))
            self.assertEqual((scan.failed, scan.skipped), ({}, set()), error)

    def test_a_required_place_that_isnt_there_is_a_failure(self):
        gone_place = self.root / ".claude"
        scan = self.search(Place(gone_place, strict=True, required=True))
        self.assertEqual(scan.failed, {str(gone_place): "it isn't there"})

    def test_a_place_that_may_be_absent_is_nothing_to_worry_about(self):
        scan = self.search(Place(self.root / "nope", strict=True), Place(self.root / "nor-this"))
        self.assertEqual((scan.failed, scan.skipped, scan.found), ({}, set(), []))

    def test_a_required_place_is_missed_even_inside_another_place(self):
        scan = self.search(Place(self.scan, strict=True), Place(self.scan / "missing", strict=True, required=True))
        self.assertEqual(list(scan.failed), [str(self.scan / "missing")])

    @NOT_ROOT
    def test_a_copy_is_found_next_to_trouble(self):
        locked = self.locked_file()
        copy = self.scan / "copy.log"
        copy.write_bytes(self.marker)
        scan = self.search(Place(self.scan, strict=True))
        self.assertEqual((scan.found, list(scan.failed)), ([copy], [str(locked)]))


class LeftoverRootsTest(unittest.TestCase):
    def setUp(self):
        self.paths = AppPaths(Path("/Users/me/Library/Application Support/GazeVoice"))
        self.home = Path.home()

    def places(self, **env):
        # Without this a Mac that sets these would change what the test sees.
        base = {"CLAUDE_CONFIG_DIR": "", "CLAUDE_CODE_TMPDIR": ""}
        with patch.dict(os.environ, {**base, **env}), \
                patch("gazevoice.voice.leftovers.tempfile.gettempdir", return_value="/var/folders/ab/cdef123/T"):
            return {place.path: place for place in leftover_roots(self.paths)}

    def test_the_places_cover_what_was_searched_by_hand(self):
        places = self.places()
        library = self.home / "Library"
        for strict, required, path in (
            (True, True, self.home / ".claude"), (True, False, self.home / ".claude.json"),
            (True, True, Path("/var/folders/ab/cdef123/T")), (True, False, Path("/tmp")), (True, True, self.paths.root),
        ):
            self.assertEqual(places[path], Place(path, strict, required), path)
        for path in (self.home / ".local", library / "Application Support", library / "Caches", library / "Logs",
                     library / "Preferences", Path("/var/folders/ab/cdef123")):  # the whole per-user folder, not only T
            self.assertEqual(places[path], Place(path), path)

    def test_a_claude_config_dir_is_searched_like_the_usual_folder(self):
        places = self.places(CLAUDE_CONFIG_DIR="/Volumes/Work/claude-config")
        configured = Path("/Volumes/Work/claude-config")
        self.assertEqual(places[configured], Place(configured, strict=True, required=True))
        usual = self.home / ".claude"
        self.assertEqual(places[usual], Place(usual, strict=True, required=False))  # may not be there if it isn't used

    def test_a_claude_code_tmpdir_is_searched_like_the_temp_folder(self):
        places = self.places(CLAUDE_CODE_TMPDIR="/Volumes/Work/cc-tmp")
        path = Path("/Volumes/Work/cc-tmp")
        self.assertEqual(places[path], Place(path, strict=True, required=True))

    def test_each_place_is_listed_once(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}):
            listed = leftover_roots(self.paths)
        self.assertEqual(len([p for p in listed if p.path == self.home / ".claude"]), 1)
        self.assertEqual(len({p.path for p in listed}), len(listed))
        self.assertTrue([p for p in listed if p.path == self.home / ".claude"][0].required)  # as strict as any mention


class LeftoverCheckTest(DescribeCropBase):
    def setUp(self):
        super().setUp()
        self.scan = self.root / "scan"
        self.scan.mkdir()
        self.job = make_job("gaze", "check_pictures")

    def tearDown(self):
        for path in self.root.rglob("*"):
            path.chmod(0o700)
        super().tearDown()

    def run_check(self, runner=None, roots=None, **env):
        return run_leftover_check(self.job, self.paths, runner=runner or claude_runner(), command=FAKE_CLAUDE,
                                  env=self.env(**env), roots=[self.scan] if roots is None else roots)

    def test_a_clean_run_records_a_pass_for_this_version(self):
        result = self.run_check()
        self.assertTrue(result["passed"])
        saved = json.loads(self.paths.gaze_picture_check.read_text())
        self.assertEqual(set(saved), {"claude", "passed", "when"})
        self.assertEqual((saved["claude"], saved["passed"]), ("2.1.303", True))
        self.assertTrue(pictures_allowed(self.paths, "2.1.303"))
        self.assertTrue(any("uses a little of your plan" in line for line in self.job.lines))

    def test_the_check_goes_through_the_same_route_as_a_real_picture(self):
        self.run_check()
        argv = json.loads(self.argv_file.read_text())
        self.assertEqual(argv, claude_session_argv(PICTURE_RULES, PICTURE_MODEL, None, max_turns=1)[1:])
        sent = json.loads(self.stdin_file.read_text())
        self.assertEqual(sent["types"], ["image", "text"])
        self.assertEqual(sent["text"], CHECK_QUESTION)  # one line, no picture.md template
        self.assertEqual(sent["media_type"], "image/png")
        self.assertEqual(os.path.realpath(sent["cwd"]), os.path.realpath(self.paths.work))
        self.assertTrue(gone(self.pid()))

    def test_the_check_waits_for_the_process_to_leave_before_it_looks_for_files(self):
        late = self.root / "late"
        late.mkdir()
        with self.assertRaises(JobError) as caught:  # a copy written only as Claude Code exits
            self.run_check(roots=[late], FAKE_CLAUDE_EXIT_DELAY="0.8", FAKE_CLAUDE_LEAK_ON_EXIT=str(late))
        self.assertIn("kept a copy", str(caught.exception))
        self.assertIn(str(next(late.iterdir()).resolve()), str(caught.exception))
        self.assertTrue(gone(self.pid()))

    def test_it_sends_a_new_random_picture_each_time(self):
        self.run_check()
        first = json.loads(self.stdin_file.read_text())["sha256"]
        self.run_check()
        self.assertNotEqual(first, json.loads(self.stdin_file.read_text())["sha256"])

    def test_a_failed_call_records_no_pass(self):
        with self.assertRaises(JobError) as caught:
            self.run_check(FAKE_CLAUDE_PICTURE="fail")
        self.assertIn("No pass", str(caught.exception))
        self.assertFalse(self.paths.gaze_picture_check.exists())

    def test_a_failed_call_leaves_an_earlier_pass_alone(self):
        write_picture_check(self.paths.gaze_picture_check, "2.1.303", 5.0)
        with self.assertRaises(JobError):
            self.run_check(FAKE_CLAUDE_PICTURE="die")
        self.assertTrue(pictures_allowed(self.paths, "2.1.303"))  # a network hiccup says nothing about leftovers

    def test_a_claude_that_says_unclear_still_passes(self):
        self.assertTrue(self.run_check(FAKE_CLAUDE_PICTURE="unclear")["passed"])

    def test_a_found_copy_is_reported_not_deleted_and_records_no_pass(self):
        leaks = self.root / "leaks"
        leaks.mkdir()
        for leak in ("FAKE_CLAUDE_LEAK_BASE64", "FAKE_CLAUDE_LEAK_RAW"):
            with self.subTest(leak):
                with self.assertRaises(JobError) as caught:
                    self.run_check(roots=[leaks], **{leak: str(leaks)})
                found = sorted(leaks.iterdir())
                self.assertEqual(len(found), 1, found)
                self.assertIn(str(found[0].resolve()), str(caught.exception))  # told where it is
                self.assertTrue(found[0].exists())  # and it is still there: it may be Claude Code's own file
                self.assertFalse(self.paths.gaze_picture_check.exists())
                found[0].unlink()

    def test_the_message_says_what_to_really_do_about_a_copy(self):
        leaks = self.root / "leaks"
        leaks.mkdir()
        with self.assertRaises(JobError) as caught:
            self.run_check(roots=[leaks], FAKE_CLAUDE_LEAK_BASE64=str(leaks))
        message = str(caught.exception)
        self.assertIn("Deleting that file won't change the result by itself", message)
        self.assertIn("every check sends a new picture", message)
        self.assertIn("leave pictures off", message)
        self.assertNotIn("until it's gone", message)  # which a rerun with a new picture would not honour

    def test_a_found_copy_cancels_an_earlier_pass_for_the_same_version(self):
        leaks = self.root / "leaks"
        leaks.mkdir()
        write_picture_check(self.paths.gaze_picture_check, "2.1.303", 5.0)
        with self.assertRaises(JobError):
            self.run_check(roots=[leaks], FAKE_CLAUDE_LEAK_BASE64=str(leaks))
        self.assertFalse(pictures_allowed(self.paths, "2.1.303"))

    def test_the_copy_is_reported_before_the_pass_is_touched_so_a_failing_removal_cant_lose_it(self):
        leaks = self.root / "leaks"
        leaks.mkdir()
        self.paths.gaze_picture_check.mkdir(parents=True)  # something that unlink can't remove
        (self.paths.gaze_picture_check / "in-the-way").write_text("x")
        with self.assertRaises(JobError) as caught:
            self.run_check(roots=[leaks], FAKE_CLAUDE_LEAK_BASE64=str(leaks))
        copy = str(next(leaks.iterdir()).resolve())
        self.assertIn(copy, str(caught.exception))
        lines = self.job.lines
        found_at = next(i for i, line in enumerate(lines) if copy in line)
        self.assertTrue(any("couldn't be removed" in line for line in lines[found_at:]))

    def test_a_copy_that_was_there_before_the_run_is_not_blamed_on_it(self):
        old = self.scan / "something-else.log"
        old.write_text("iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAIAAADTED8xAAAAA")  # looks like a picture's start, from long ago
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.assertTrue(self.run_check()["passed"])

    # --- a search that couldn't look is not a pass ---------------------------------------------------------------
    def locked_file(self, where):
        path = where / "locked.bin"
        path.write_bytes(b"something that is long enough to be worth opening " * 10)
        path.chmod(0)
        return path

    @NOT_ROOT
    def test_a_search_that_could_not_look_records_no_pass(self):
        locked = self.locked_file(self.scan)
        with self.assertRaises(JobError) as caught:
            self.run_check(roots=[Place(self.scan, strict=True)])
        message = str(caught.exception)
        self.assertIn("couldn't look everywhere it has to", message)
        self.assertIn(str(locked.resolve()), message)
        self.assertIn("No pass was recorded", message)
        self.assertFalse(self.paths.gaze_picture_check.exists())
        self.assertTrue(any("Couldn't look through" in line and str(locked.resolve()) in line for line in self.job.lines))

    @NOT_ROOT
    def test_a_search_that_could_not_look_leaves_an_earlier_pass_alone(self):
        self.locked_file(self.scan)
        write_picture_check(self.paths.gaze_picture_check, "2.1.303", 5.0)
        with self.assertRaises(JobError):
            self.run_check(roots=[Place(self.scan, strict=True)])
        self.assertTrue(pictures_allowed(self.paths, "2.1.303"))  # no evidence either way

    @NOT_ROOT
    def test_places_macos_keeps_private_are_counted_and_the_check_still_passes(self):
        self.locked_file(self.scan)
        self.assertTrue(self.run_check(roots=[Place(self.scan)])["passed"])
        self.assertTrue(any("1 files or folders" in line and "don't count against the check" in line
                            for line in self.job.lines))

    def test_a_required_place_that_isnt_there_fails_the_check(self):
        with self.assertRaises(JobError) as caught:
            self.run_check(roots=[Place(self.scan), Place(self.root / ".claude", strict=True, required=True)])
        self.assertIn("it isn't there", str(caught.exception))
        self.assertFalse(self.paths.gaze_picture_check.exists())

    def test_a_found_copy_is_what_is_reported_when_there_was_trouble_as_well(self):
        leaks = self.root / "leaks"
        leaks.mkdir()
        write_picture_check(self.paths.gaze_picture_check, "2.1.303", 5.0)
        with self.assertRaises(JobError) as caught:
            self.run_check(roots=[Place(leaks, strict=True), Place(self.root / "nope", strict=True, required=True)],
                           FAKE_CLAUDE_LEAK_BASE64=str(leaks))
        self.assertIn("kept a copy", str(caught.exception))
        self.assertFalse(pictures_allowed(self.paths, "2.1.303"))

    def test_claude_not_installed_or_logged_out_sends_nothing(self):
        for runner, words in ((claude_runner(installed=False, version=None, auth=None), "isn't installed"),
                              (claude_runner(auth=LOGGED_OUT), "log")):
            with self.subTest(words):
                with self.assertRaises(JobError) as caught:
                    self.run_check(runner)
                self.assertIn(words, str(caught.exception).lower())
                self.assertFalse(self.stdin_file.exists())
                self.assertFalse(self.paths.gaze_picture_check.exists())

    def test_the_log_never_holds_the_picture(self):
        self.run_check()
        text = "\n".join(self.job.lines)
        self.assertNotIn("iVBOR", text)
        self.assertNotIn(self.picture.base64[:40], text)

    def test_the_picture_is_made_in_memory(self):
        before = tree(self.paths.root)
        self.run_check()
        after = tree(self.paths.root)
        self.assertEqual({k for k in after if k not in before}, {"state", "state/gaze-picture-check.json"})
        self.assertEqual([p for p in self.paths.root.rglob("*") if p.suffix in (".png", ".b64")], [])


class NoisePictureTest(unittest.TestCase):
    def test_it_is_a_real_png_with_fingerprints_that_are_inside_it(self):
        picture = make_noise_picture(64)
        self.assertEqual(picture.png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(base64.b64decode(picture.base64), picture.png)
        literal, middle, prefix = picture.markers
        self.assertEqual(len(literal), 256)
        self.assertIn(literal, picture.png)
        self.assertIn(middle, picture.base64.encode())
        self.assertEqual(prefix, picture.base64.encode()[:96])
        self.assertEqual(len(middle), 64)
        self.assertNotEqual(make_noise_picture(64).png, picture.png)

    def test_the_fingerprints_do_not_match_an_ordinary_picture(self):
        picture = make_noise_picture(64)
        other = make_noise_picture(64)
        self.assertNotIn(picture.markers[0], other.png)
        self.assertNotIn(picture.markers[1], other.base64.encode())
        self.assertNotIn(picture.markers[2], other.base64.encode())  # reaches past the header every such picture shares


if __name__ == "__main__":
    unittest.main()
