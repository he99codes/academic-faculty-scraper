"""
Unit and Integration Test Suite for the Academic Scraper.
Tests data validation, hierarchy scoring, domain resolution heuristics, and DOM extraction.
"""

import unittest
from bs4 import BeautifulSoup

from models import FacultyMember, InstitutionReport
from domain_resolver import is_aggregator, decode_bing_url, score_domain_match
from extractor import FacultyExtractor, is_probable_name, DESIGNATION_PATTERN


class TestModels(unittest.TestCase):
    def test_hierarchy_ranking(self):
        # Executive leadership
        self.assertEqual(FacultyMember.compute_hierarchy_rank("President of Stanford University"), 100)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Director"), 100)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Vice Chancellor"), 100)

        # Deans
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Dean of Engineering"), 85)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Senior Associate Dean"), 85)

        # HODs / Chairs
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Head of Department"), 75)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Department Chair"), 75)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("HOD Computer Science"), 75)

        # Professors
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Distinguished Professor"), 65)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Professor of Computer Science"), 50)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Associate Professor"), 40)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Assistant Professor"), 30)
        self.assertEqual(FacultyMember.compute_hierarchy_rank("Lecturer"), 20)

    def test_faculty_member_validation(self):
        member = FacultyMember(
            name="  Dr. John Doe, Ph.D.  ",
            designation=" Dean of Science ",
            department="School of Science",
            profile_url="https://example.edu/faculty/jdoe",
            institution="Example University",
            source_url="https://example.edu/faculty",
            rank_score=85
        )
        self.assertEqual(member.name, "Dr. John Doe, Ph.D.")
        self.assertEqual(member.designation, "Dean of Science")
        self.assertEqual(member.rank_score, 85)


class TestDomainResolver(unittest.TestCase):
    def test_aggregator_blocklist(self):
        self.assertTrue(is_aggregator("https://en.wikipedia.org/wiki/Stanford_University"))
        self.assertTrue(is_aggregator("https://www.linkedin.com/school/stanford-university/"))
        self.assertTrue(is_aggregator("https://www.shiksha.com/university/stanford"))
        self.assertFalse(is_aggregator("https://www.stanford.edu/"))
        self.assertFalse(is_aggregator("https://cs.iitb.ac.in/"))

    def test_domain_scoring(self):
        stanford_score = score_domain_match("https://www.stanford.edu/", "Stanford University")
        unrelated_score = score_domain_match("https://www.randomsite.com/", "Stanford University")
        self.assertGreater(stanford_score, unrelated_score)

    def test_bing_url_decoding(self):
        # Encoded https://www.stanford.edu/
        raw_bing = "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly93d3cuc3RhbmZvcmQuZWR1Lw&ntb=1"
        decoded = decode_bing_url(raw_bing)
        self.assertEqual(decoded, "https://www.stanford.edu/")


class TestExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = FacultyExtractor(institution_name="Test Tech")

    def test_is_probable_name(self):
        self.assertTrue(is_probable_name("Dr. Alice Smith"))
        self.assertTrue(is_probable_name("Prof. Bob R. Johnson"))
        self.assertTrue(is_probable_name("David M. Patterson"))
        self.assertFalse(is_probable_name("View Profile"))
        self.assertFalse(is_probable_name("Department of Biology"))
        self.assertFalse(is_probable_name("Next Page"))

    def test_html_card_extraction(self):
        mock_html = """
        <div class="directory-list">
            <div class="faculty-card">
                <h3><a href="/faculty/alice">Dr. Alice Morgan</a></h3>
                <span class="role">Dean of Computer Science</span>
                <p class="dept">Department of Computer Science</p>
                <a href="mailto:alice.m@test.edu">alice.m@test.edu</a>
                <p class="bio">Research in Artificial Intelligence and Quantum Computing systems.</p>
            </div>
            <div class="faculty-card">
                <h3><a href="/faculty/bob">Prof. Robert Chen</a></h3>
                <span class="role">Head of Department</span>
                <p class="dept">Electrical Engineering</p>
                <a href="mailto:rchen@test.edu">rchen@test.edu</a>
                <p class="bio">Focusing on Photonic Integrated Circuits and VLSI Architecture.</p>
            </div>
            <div class="faculty-card">
                <h3><a href="/faculty/clara">Dr. Clara Oswald</a></h3>
                <span class="role">Assistant Professor</span>
                <p class="dept">Physics Department</p>
                <a href="mailto:clara@test.edu">clara@test.edu</a>
                <p class="bio">Astrophysics and Dark Matter observations.</p>
            </div>
        </div>
        """
        members = self.extractor.extract_from_html(mock_html, "https://test.edu/people")
        self.assertEqual(len(members), 3)

        # Check Dean
        dean = members[0]
        self.assertIn("Alice Morgan", dean.name)
        self.assertEqual(dean.designation, "Dean of Computer Science")
        self.assertEqual(dean.email, "alice.m@test.edu")
        self.assertEqual(dean.profile_url, "https://test.edu/faculty/alice")
        self.assertEqual(dean.rank_score, 85)

        # Check HOD
        hod = members[1]
        self.assertIn("Robert Chen", hod.name)
        self.assertEqual(hod.designation, "Head of Department")
        self.assertEqual(hod.email, "rchen@test.edu")
        self.assertEqual(hod.rank_score, 75)

        # Check Assistant Professor
        asst = members[2]
        self.assertEqual(asst.designation, "Assistant Professor")
        self.assertEqual(asst.rank_score, 30)


if __name__ == "__main__":
    unittest.main()
