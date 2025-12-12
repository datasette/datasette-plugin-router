import {postDemo1} from "./generated_client/sdk.gen.ts";

const button = document.createElement("button");
button.innerText = "Call demo1 API";
button.onclick = async () => {
  button.disabled = true;
  button.innerText = "Calling...";
  try {
    const response = await postDemo1({
      body: { id: 123, name: "hello world" }
    });
    alert(`Response: ${JSON.stringify(response.data)}`);
  } catch (e) {
    alert(`Error: ${e}`);
  } finally {
    button.disabled = false;
    button.innerText = "Call demo1 API";
  }
};
document.body.appendChild(button);
