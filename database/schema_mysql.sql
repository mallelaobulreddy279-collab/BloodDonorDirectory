-- ============================================================================
--  Digital Blood Donor Directory  |  MySQL 8 schema (3NF)
--  The bundled web app runs on SQLite so it works with zero setup. This file is
--  the same design for a MySQL deployment (as named in the project report).
--  Usage:  mysql -u root -p < schema_mysql.sql
-- ============================================================================
CREATE DATABASE IF NOT EXISTS BloodDonationDB CHARACTER SET utf8mb4;
USE BloodDonationDB;

-- ---------- DDL ------------------------------------------------------------
CREATE TABLE Cities (
    city_id    INT AUTO_INCREMENT PRIMARY KEY,
    city_name  VARCHAR(50) NOT NULL UNIQUE
);

CREATE TABLE Sectors (
    sector_id   INT AUTO_INCREMENT PRIMARY KEY,
    city_id     INT NOT NULL,
    sector_name VARCHAR(100) NOT NULL,
    UNIQUE (city_id, sector_name),
    FOREIGN KEY (city_id) REFERENCES Cities(city_id) ON DELETE CASCADE
);

CREATE TABLE Blood_Groups (
    blood_group_id TINYINT PRIMARY KEY,
    group_name     VARCHAR(5) NOT NULL UNIQUE
);

CREATE TABLE Users (
    user_id       INT AUTO_INCREMENT PRIMARY KEY,
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(120) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role          ENUM('donor','recipient','admin') NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- "Master" table
CREATE TABLE Donors (
    donor_id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id            INT NOT NULL UNIQUE,
    blood_group_id     TINYINT NOT NULL,
    sector_id          INT NOT NULL,
    contact_no         VARCHAR(15) NOT NULL UNIQUE,
    age                INT NOT NULL CHECK (age >= 18),
    weight_kg          INT NOT NULL CHECK (weight_kg >= 45),
    last_donation_date DATE NULL,
    status             ENUM('Available','Busy','Traveling') NOT NULL DEFAULT 'Available',
    registered_on      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id)        REFERENCES Users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (blood_group_id) REFERENCES Blood_Groups(blood_group_id),
    FOREIGN KEY (sector_id)      REFERENCES Sectors(sector_id),
    INDEX idx_group_sector (blood_group_id, sector_id)       -- NF1: fast sector search
);

-- "Slave" table
CREATE TABLE Donation_History (
    donation_id   INT AUTO_INCREMENT PRIMARY KEY,
    donor_id      INT NOT NULL,
    donation_date DATE NOT NULL,
    volume_ml     INT NOT NULL CHECK (volume_ml BETWEEN 200 AND 500),
    location      VARCHAR(100) NOT NULL,
    status        ENUM('Completed','Screened','Pending') NOT NULL DEFAULT 'Completed',
    FOREIGN KEY (donor_id) REFERENCES Donors(donor_id) ON DELETE CASCADE
);

CREATE TABLE Emergency_Requests (
    request_id     INT AUTO_INCREMENT PRIMARY KEY,
    requester_id   INT NOT NULL,
    patient_name   VARCHAR(80) NOT NULL,
    blood_group_id TINYINT NOT NULL,
    sector_id      INT NOT NULL,
    hospital       VARCHAR(100) NOT NULL,
    units          INT NOT NULL CHECK (units BETWEEN 1 AND 10),
    urgency_level  ENUM('Critical','Urgent','Planned') NOT NULL,
    contact_no     VARCHAR(15) NOT NULL,
    note           VARCHAR(300),
    status         ENUM('Pending','Verified','Fulfilled','Closed') NOT NULL DEFAULT 'Pending',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (requester_id)   REFERENCES Users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (blood_group_id) REFERENCES Blood_Groups(blood_group_id),
    FOREIGN KEY (sector_id)      REFERENCES Sectors(sector_id),
    INDEX idx_req_sector (sector_id, status)
);

CREATE TABLE Request_Responses (
    response_id  INT AUTO_INCREMENT PRIMARY KEY,
    request_id   INT NOT NULL,
    donor_id     INT NOT NULL,
    responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (request_id, donor_id),
    FOREIGN KEY (request_id) REFERENCES Emergency_Requests(request_id) ON DELETE CASCADE,
    FOREIGN KEY (donor_id)   REFERENCES Donors(donor_id) ON DELETE CASCADE
);

CREATE TABLE Notifications (
    notification_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT NOT NULL,
    request_id INT NULL,
    message    VARCHAR(400) NOT NULL,
    is_read    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id)    REFERENCES Users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (request_id) REFERENCES Emergency_Requests(request_id) ON DELETE CASCADE
);

CREATE TABLE Contact_Log (            -- audit trail of phone-number reveals
    log_id    INT AUTO_INCREMENT PRIMARY KEY,
    viewer_id INT NOT NULL,
    donor_id  INT NOT NULL,
    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (viewer_id) REFERENCES Users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (donor_id)  REFERENCES Donors(donor_id) ON DELETE CASCADE
);

-- Real-time update: a completed donation refreshes Donors.last_donation_date
DELIMITER //
CREATE TRIGGER trg_donation_updates_last
AFTER INSERT ON Donation_History
FOR EACH ROW
BEGIN
    IF NEW.status = 'Completed' THEN
        UPDATE Donors
           SET last_donation_date = (SELECT MAX(donation_date) FROM Donation_History
                                      WHERE donor_id = NEW.donor_id AND status = 'Completed')
         WHERE donor_id = NEW.donor_id;
    END IF;
END//
DELIMITER ;

-- Public search view: the 90-Day Automated Eligibility Logic (SRS F3)
CREATE VIEW v_available_donors AS
SELECT d.donor_id, u.full_name, bg.group_name AS blood_group, c.city_name, s.sector_name,
       d.last_donation_date, d.contact_no
  FROM Donors d
  JOIN Users u         ON u.user_id = d.user_id
  JOIN Blood_Groups bg ON bg.blood_group_id = d.blood_group_id
  JOIN Sectors s       ON s.sector_id = d.sector_id
  JOIN Cities c        ON c.city_id = s.city_id
 WHERE d.status = 'Available'
   AND (d.last_donation_date IS NULL OR d.last_donation_date <= DATE_SUB(CURDATE(), INTERVAL 90 DAY));

-- ---------- Reference data ---------------------------------------------------
INSERT INTO Blood_Groups VALUES (1,'A+'),(2,'A-'),(3,'B+'),(4,'B-'),(5,'AB+'),(6,'AB-'),(7,'O+'),(8,'O-');
INSERT INTO Cities (city_name) VALUES ('Madanapalle'),('Tirupati'),('Chittoor');
INSERT INTO Sectors (city_id, sector_name) VALUES
 (1,'Downtown'),(1,'Industrial Area'),(1,'North Sector'),(1,'East Sector'),(1,'South Sector'),(1,'Medical Quarter'),
 (2,'Temple District'),(2,'Railway Colony'),(2,'University Sector'),(2,'Airport Road'),(2,'Old Town'),(2,'Renigunta Belt'),
 (3,'Collectorate Area'),(3,'Market Yard'),(3,'Gandhi Road'),(3,'Bypass Industrial Estate'),(3,'Green Valley'),(3,'Hospital Road');

-- ---------- DCL ------------------------------------------------------------
CREATE USER IF NOT EXISTS 'bdd_app'@'localhost' IDENTIFIED BY 'ChangeMe#2026';
GRANT SELECT, INSERT, UPDATE ON BloodDonationDB.* TO 'bdd_app'@'localhost';
REVOKE UPDATE ON BloodDonationDB.Contact_Log FROM 'bdd_app'@'localhost';
