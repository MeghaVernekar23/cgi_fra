"""
Selenium script: open CGI Frankfurt visa appointment site,
tick the agreement checkbox, click Proceed.

Run:
    uv run main.py
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = "https://appointment.cgifrankfurt.gov.in/"


def main() -> None:
    driver = webdriver.Chrome()
    wait = WebDriverWait(driver, 20)

    try:
        driver.get(URL)

        # Tick the checkbox (usually an "I agree" / terms checkbox before Proceed)
        checkbox = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='checkbox']"))
        )
        if not checkbox.is_selected():
            driver.execute_script("arguments[0].click();", checkbox)

        # Click Proceed button (match by visible text, case-insensitive)
        proceed_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(translate(., 'PROCEED', 'proceed'), 'proceed')] "
                           "| //input[@type='submit' and contains(translate(@value,'PROCEED','proceed'),'proceed')]")
            )
        )
        proceed_btn.click()

        # Give the next page a moment to load
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

    finally:
        input("Press Enter to close browser...")
        driver.quit()


if __name__ == "__main__":
    main()
