-- Sample SQL for the project report (DDL/DML/DCL/DQL, aggregates, joins, sub-queries)
-- Written against schema_mysql.sql. Run after loading the schema and some donors.
USE BloodDonationDB;

-- DML
UPDATE Donors SET sector_id = 2 WHERE donor_id = 1;                       -- donor moves sector
INSERT INTO Donation_History (donor_id, donation_date, volume_ml, location)
VALUES (1, CURDATE(), 450, 'Madanapalle Government Blood Bank');          -- trigger updates last_donation_date

-- DQL: sector-based search (F4) - eligible O+ donors in Downtown, Madanapalle
SELECT full_name, blood_group, sector_name,
       CONCAT(LEFT(contact_no,2), '••• ••', RIGHT(contact_no,3)) AS masked_contact
  FROM v_available_donors
 WHERE blood_group = 'O+' AND city_name = 'Madanapalle' AND sector_name = 'Downtown';

-- Donors hidden by the 90-day rule, with the date they return
SELECT u.full_name, d.last_donation_date,
       DATE_ADD(d.last_donation_date, INTERVAL 90 DAY) AS eligible_on,
       DATEDIFF(CURDATE(), d.last_donation_date)       AS days_since
  FROM Donors d JOIN Users u ON u.user_id = d.user_id
 WHERE DATEDIFF(CURDATE(), d.last_donation_date) < 90;

-- Aggregates
SELECT COUNT(*) AS total_donors FROM Donors;
SELECT bg.group_name, COUNT(*) AS donors
  FROM Donors d JOIN Blood_Groups bg USING (blood_group_id) GROUP BY bg.group_name ORDER BY donors DESC;
SELECT AVG(age) AS avg_age, MIN(age) AS youngest, MAX(age) AS oldest FROM Donors;

-- Joins
SELECT c.city_name, s.sector_name, COUNT(d.donor_id) AS donors            -- LEFT JOIN keeps empty sectors
  FROM Sectors s JOIN Cities c USING (city_id) LEFT JOIN Donors d USING (sector_id)
 GROUP BY c.city_name, s.sector_name;

SELECT er.request_id, er.hospital, u.full_name AS donor_who_responded    -- INNER JOIN
  FROM Emergency_Requests er
  JOIN Request_Responses rr USING (request_id)
  JOIN Donors d ON d.donor_id = rr.donor_id
  JOIN Users u  ON u.user_id = d.user_id;

-- Sub-queries
SELECT full_name FROM Users
 WHERE user_id IN (SELECT user_id FROM Donors WHERE age > (SELECT AVG(age) FROM Donors));

SELECT sector_name FROM Sectors
 WHERE sector_id = (SELECT sector_id FROM Emergency_Requests WHERE request_id = 1);

-- Sectors with open critical requests but no eligible donor (needs outreach)
SELECT s.sector_name FROM Sectors s
 WHERE s.sector_id IN (SELECT sector_id FROM Emergency_Requests
                        WHERE urgency_level = 'Critical' AND status IN ('Pending','Verified'))
   AND s.sector_id NOT IN (SELECT d.sector_id FROM Donors d JOIN v_available_donors v USING (donor_id));
