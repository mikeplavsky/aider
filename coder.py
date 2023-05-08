#!/usr/bin/env python

import os
import sys
import copy
import random
import json
import re
import readline
import traceback
import argparse

from tqdm import tqdm

from pathlib import Path
from collections import defaultdict
from pygments import highlight, lexers, formatters

import os
import openai

from dump import dump

import prompts

history_file = ".coder.history"
try:
    readline.read_history_file(history_file)
except FileNotFoundError:
    pass

formatter = formatters.TerminalFormatter()

openai.api_key = os.getenv("OPENAI_API_KEY")


def find_index(list1, list2):
    for i in range(len(list1)):
        if list1[i : i + len(list2)] == list2:
            return i
    return -1


class Coder:
    fnames = dict()
    last_modified = 0

    def __init__(self, use_gpt_4, files):
        if use_gpt_4:
            self.main_model = "gpt-4"
        else:
            self.main_model = "gpt-3.5-turbo"

        for fname in files:
            self.fnames[fname] = Path(fname).stat().st_mtime

        self.check_for_local_edits(True)

    def files_modified(self):
        for fname, mtime in self.fnames.items():
            if Path(fname).stat().st_mtime != mtime:
                return True

    def request(self, prompt):
        self.request_prompt = prompt

    def quoted_file(self, fname):
        prompt = "\n"
        prompt += fname
        prompt += "\n```\n"
        prompt += Path(fname).read_text()
        prompt += "\n```\n"
        return prompt

    def get_files_content(self):
        prompt = ""
        for fname in self.fnames:
            prompt += self.quoted_file(fname)
        return prompt

    def get_input(self):
        print()
        print("=" * 60)
        inp = ""
        num_control_c = 0
        while not inp.strip():
            try:
                inp = input("> ")
            except EOFError:
                return
            except KeyboardInterrupt:
                num_control_c += 1
                print()
                if num_control_c >= 2:
                    return
                print("^C again to quit")

        print()

        readline.write_history_file(history_file)
        return inp

    def check_for_local_edits(self, init=False):
        last_modified = max(Path(fname).stat().st_mtime for fname in self.fnames)
        since = last_modified - self.last_modified
        self.last_modified = last_modified
        if init:
            return
        if since > 0:
            return True
        return False

    def get_files_messages(self):
        files_content = prompts.files_content_prefix
        files_content += self.get_files_content()
        files_content += prompts.files_content_suffix

        files_messages = [
            dict(role="user", content=files_content),
            dict(role="assistant", content="Ok."),
        ]

        return files_messages

    def run(self):
        self.done_messages = []
        self.cur_messages = []

        while True:
            inp = self.get_input()
            if inp is None:
                return

            if self.check_for_local_edits():
                # files changed, move cur messages back behind the files messages
                self.done_messages += self.cur_messages
                self.done_messages += [
                    dict(role="user", content=prompts.files_content_local_edits),
                    dict(role="assistant", content="Ok."),
                ]
                self.cur_messages = []

            self.cur_messages += [
                dict(role="user", content=inp),
            ]

            # self.show_messages(self.done_messages, "done")
            # self.show_messages(self.files_messages, "files")
            # self.show_messages(self.cur_messages, "cur")

            messages = [
                dict(role="system", content=prompts.main_system),
            ]
            messages += self.done_messages
            messages += self.get_files_messages()
            messages += self.cur_messages

            self.show_messages(messages, "all")

            content = self.send(messages)

            self.cur_messages += [
                dict(role="assistant", content=content),
            ]

            print()
            print()
            try:
                edited = self.update_files(content, inp)
            except Exception as err:
                print(err)
                print()
                traceback.print_exc()
                edited = None

            if not edited:
                continue

            self.check_for_local_edits(True)
            self.done_messages += self.cur_messages
            self.done_messages += [
                dict(role="user", content=prompts.files_content_gpt_edits),
                dict(role="assistant", content="Ok."),
            ]
            self.cur_messages = []

    def show_messages(self, messages, title):
        print(title.upper(), "*" * 50)

        for msg in messages:
            print()
            print("-" * 50)
            role = msg["role"].upper()
            content = msg["content"].splitlines()
            for line in content:
                print(role, line)

    def send(self, messages, model=None, show_progress=0):
        # self.show_messages(messages, "all")

        if not model:
            model = self.main_model

        completion = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=0,
            stream=True,
        )

        if show_progress:
            return self.show_send_progress(completion, show_progress)
        else:
            return self.show_send_output_plain(completion)

    def show_send_progress(self, completion, show_progress):
        resp = []
        pbar = tqdm(total=show_progress)
        for chunk in completion:
            try:
                text = chunk.choices[0].delta.content
                resp.append(text)
            except AttributeError:
                continue

            pbar.update(len(text))

        pbar.update(show_progress)
        pbar.close()

        resp = "".join(resp)
        return resp

    def show_send_output_plain(self, completion):
        resp = ""

        in_diff = False
        diff_lines = []

        partial_line = ""
        for chunk in completion:
            if chunk.choices[0].finish_reason not in (None, "stop"):
                dump(chunk.choices[0].finish_reason)
            try:
                text = chunk.choices[0].delta.content
                resp += text
            except AttributeError:
                continue

            sys.stdout.write(text)
            sys.stdout.flush()

            # disabled
            if False and "```" in resp:
                return resp

        return resp

    def show_send_output_color(self, completion):
        resp = []

        in_diff = False
        diff_lines = []

        def print_lines():
            if not diff_lines:
                return
            code = "\n".join(diff_lines)
            lexer = lexers.guess_lexer(code)
            code = highlight(code, lexer, formatter)
            print(code, end="")

        partial_line = ""
        for chunk in completion:
            try:
                text = chunk.choices[0].delta.content
                resp.append(text)
            except AttributeError:
                continue

            lines = partial_line + text
            lines = lines.split("\n")
            partial_line = lines.pop()

            for line in lines:
                check = line.rstrip()
                if check == ">>>>>>> UPDATED":
                    print_lines()
                    in_diff = False
                    diff_lines = []

                if check == "=======":
                    print_lines()
                    diff_lines = []
                    print(line)
                elif in_diff:
                    diff_lines.append(line)
                else:
                    print(line)

                if line.strip() == "<<<<<<< ORIGINAL":
                    in_diff = True
                    diff_lines = []

        print_lines()
        if partial_line:
            print(partial_line)

        return "".join(resp)

    pattern = re.compile(
        r"(\S+)\s+(```)?<<<<<<< ORIGINAL\n(.*?\n?)=======\n(.*?\n?)>>>>>>> UPDATED",
        re.MULTILINE | re.DOTALL,
    )

    def update_files(self, content, inp):
        edited = set()
        for match in self.pattern.finditer(content):
            path, _, original, updated = match.groups()

            edited.add(path)
            if self.do_replace(path, original, updated):
                continue
            edit = match.group()
            self.do_gpt_powered_replace(path, edit, inp)

        return edited

    def do_replace(self, fname, before_text, after_text):
        before_text = self.strip_quoted_wrapping(before_text, fname)
        after_text = self.strip_quoted_wrapping(after_text, fname)

        fname = Path(fname)

        # does it want to make a new file?
        if not fname.exists() and not before_text:
            print("Creating empty file:", fname)
            fname.touch()

        content = fname.read_text().splitlines()

        if not before_text and not content:
            # first populating an empty file
            new_content = after_text
        else:
            before_lines = [l.strip() for l in before_text.splitlines()]
            stripped_content = [l.strip() for l in content]
            where = find_index(stripped_content, before_lines)

            if where < 0:
                return

            new_content = content[:where]
            new_content += after_text.splitlines()
            new_content += content[where + len(before_lines) :]
            new_content = "\n".join(new_content) + "\n"

        fname.write_text(new_content)
        print("Applied edit to", fname)
        return True

    def do_gpt_powered_replace(self, fname, edit, request):
        model = "gpt-3.5-turbo"
        print(f"Asking {model} to apply ambiguous edit to {fname}...")

        fname = Path(fname)
        content = fname.read_text()
        prompt = prompts.editor_user.format(
            request=request,
            edit=edit,
            fname=fname,
            content=content,
        )

        messages = [
            dict(role="system", content=prompts.editor_system),
            dict(role="user", content=prompt),
        ]
        res = self.send(
            messages, show_progress=len(content) + len(edit) / 2, model=model
        )
        res = self.strip_quoted_wrapping(res, fname)
        fname.write_text(res)

    def strip_quoted_wrapping(self, res, fname=None):
        if not res:
            return res

        res = res.splitlines()

        if fname and res[0].strip().endswith(Path(fname).name):
            res = res[1:]

        if res[0].startswith("```") and res[-1].startswith("```"):
            res = res[1:-1]

        res = "\n".join(res)
        if res and res[-1] != "\n":
            res += "\n"

        return res


def main():
    parser = argparse.ArgumentParser(description="Chat with GPT about code")
    parser.add_argument(
        "files", metavar="FILE", nargs="+", help="a list of source code files"
    )
    parser.add_argument(
        "-3",
        "--gpt-3-5-turbo",
        action="store_true",
        help="Only use gpt-3.5-turbo, not gpt-4",
    )

    args = parser.parse_args()

    use_gpt_4 = not args.gpt_3_5_turbo
    fnames = args.files

    coder = Coder(use_gpt_4, fnames)
    coder.run()


if __name__ == "__main__":
    status = main()
    sys.exit(status)
