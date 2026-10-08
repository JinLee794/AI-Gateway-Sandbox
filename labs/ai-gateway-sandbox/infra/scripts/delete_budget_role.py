"""azd postdown hook: delete the custom role definition of the "over budget, switched off" Logic App.

Role definitions live at the subscription level, so 'azd down' (which deletes the resource group and with it the
Logic App's role assignment) leaves the definition behind. azd passes the BUDGET_SUSPEND_CUSTOM_ROLE_ID output of
infra/main.bicep to the hook; it is empty when budgetSuspend is disabled or uses the built-in role.
"""
import os
import shutil
import subprocess
import sys

role_id = os.environ.get("BUDGET_SUSPEND_CUSTOM_ROLE_ID", "").strip()
if not role_id:
    print("No custom budget-suspend role to delete.")
    sys.exit(0)

# DELETE the role definition by its full id. 'az role definition delete --name <guid>' looks the role up first and can
# exit 0 without deleting anything when the lookup returns nothing.
url = f"https://management.azure.com{role_id}?api-version=2022-04-01"
command = ["az", "rest", "--method", "delete", "--url", url]
az = shutil.which("az")
if not az:
    print(f"Azure CLI not found. Delete the custom role manually: {' '.join(command)}")
    sys.exit(0)
result = subprocess.run([az, *command[1:]], capture_output=True, text=True)
if result.returncode == 0:
    print(f"Deleted the custom role definition {role_id.split('/')[-1]}.")
else:
    print(f"Could not delete the custom role definition ({result.stderr.strip()[:300]}). Delete it manually: {' '.join(command)}")
