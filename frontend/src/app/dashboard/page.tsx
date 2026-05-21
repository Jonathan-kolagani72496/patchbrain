
Search

May 21, 2:36 AM - 2:40 AM
CDT



==> Downloading cache...
==> Cloning from https://github.com/Jonathan-kolagani72496/patchbrain
==> Checking out commit d3e55b5c0572c5e73ddda3169edca38ad125cc2e in branch main
==> Downloaded 63MB in 2s. Extraction took 0s.
==> Using Python version 3.11.11 via environment variable PYTHON_VERSION
==> Docs on specifying a Python version: https://render.com/docs/python-version
==> Installing Python version 3.11.11...
==> Using Poetry version 2.1.3 (default)
==> Docs on specifying a Poetry version: https://render.com/docs/poetry-version
==> Running build command 'pip install -r requirements.txt'...
Collecting fastapi==0.111.0 (from -r requirements.txt (line 2))
  Using cached fastapi-0.111.0-py3-none-any.whl.metadata (25 kB)
Collecting uvicorn==0.29.0 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached uvicorn-0.29.0-py3-none-any.whl.metadata (6.3 kB)
Collecting python-multipart==0.0.9 (from -r requirements.txt (line 4))
  Using cached python_multipart-0.0.9-py3-none-any.whl.metadata (2.5 kB)
Collecting supabase==2.5.0 (from -r requirements.txt (line 7))
  Using cached supabase-2.5.0-py3-none-any.whl.metadata (9.3 kB)
Collecting asyncpg==0.29.0 (from -r requirements.txt (line 8))
  Using cached asyncpg-0.29.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (4.4 kB)
Collecting PyMuPDF==1.24.3 (from -r requirements.txt (line 11))
  Using cached PyMuPDF-1.24.3-cp311-none-manylinux2014_x86_64.whl.metadata (3.4 kB)
Collecting openpyxl==3.1.2 (from -r requirements.txt (line 12))
  Using cached openpyxl-3.1.2-py2.py3-none-any.whl.metadata (2.5 kB)
Collecting python-docx==1.1.2 (from -r requirements.txt (line 13))
  Using cached python_docx-1.1.2-py3-none-any.whl.metadata (2.0 kB)
Collecting duckdb==0.10.3 (from -r requirements.txt (line 14))
  Using cached duckdb-0.10.3-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (763 bytes)
Collecting openai==1.30.0 (from -r requirements.txt (line 17))
  Using cached openai-1.30.0-py3-none-any.whl.metadata (21 kB)
Collecting anthropic==0.57.1 (from -r requirements.txt (line 20))
  Using cached anthropic-0.57.1-py3-none-any.whl.metadata (27 kB)
Collecting httpx==0.27.0 (from -r requirements.txt (line 23))
  Using cached httpx-0.27.0-py3-none-any.whl.metadata (7.2 kB)
Collecting python-dotenv==1.0.1 (from -r requirements.txt (line 24))
  Using cached python_dotenv-1.0.1-py3-none-any.whl.metadata (23 kB)
Collecting pydantic==2.7.2 (from -r requirements.txt (line 25))
  Using cached pydantic-2.7.2-py3-none-any.whl.metadata (108 kB)
Collecting pydantic-settings==2.3.0 (from -r requirements.txt (line 26))
  Using cached pydantic_settings-2.3.0-py3-none-any.whl.metadata (3.3 kB)
Collecting starlette<0.38.0,>=0.37.2 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached starlette-0.37.2-py3-none-any.whl.metadata (5.9 kB)
Collecting typing-extensions>=4.8.0 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached typing_extensions-4.15.0-py3-none-any.whl.metadata (3.3 kB)
Collecting fastapi-cli>=0.0.2 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached fastapi_cli-0.0.24-py3-none-any.whl.metadata (6.4 kB)
Collecting jinja2>=2.11.2 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached jinja2-3.1.6-py3-none-any.whl.metadata (2.9 kB)
Collecting ujson!=4.0.2,!=4.1.0,!=4.2.0,!=4.3.0,!=5.0.0,!=5.1.0,>=4.0.1 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached ujson-5.12.1-cp311-cp311-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl.metadata (9.6 kB)
Collecting orjson>=3.2.1 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached orjson-3.11.9-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (41 kB)
Collecting email_validator>=2.0.0 (from fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached email_validator-2.3.0-py3-none-any.whl.metadata (26 kB)
Collecting click>=7.0 (from uvicorn==0.29.0->uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached click-8.4.0-py3-none-any.whl.metadata (2.6 kB)
Collecting h11>=0.8 (from uvicorn==0.29.0->uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
Collecting gotrue<3.0,>=1.3 (from supabase==2.5.0->-r requirements.txt (line 7))
  Using cached gotrue-2.12.4-py3-none-any.whl.metadata (2.0 kB)
Collecting postgrest<0.17.0,>=0.14 (from supabase==2.5.0->-r requirements.txt (line 7))
  Using cached postgrest-0.16.11-py3-none-any.whl.metadata (5.1 kB)
Collecting realtime<2.0.0,>=1.0.0 (from supabase==2.5.0->-r requirements.txt (line 7))
  Using cached realtime-1.0.6-py3-none-any.whl.metadata (2.6 kB)
Collecting storage3<0.8.0,>=0.5.3 (from supabase==2.5.0->-r requirements.txt (line 7))
  Using cached storage3-0.7.7-py3-none-any.whl.metadata (1.9 kB)
Collecting supafunc<0.5.0,>=0.3.1 (from supabase==2.5.0->-r requirements.txt (line 7))
  Using cached supafunc-0.4.7-py3-none-any.whl.metadata (1.2 kB)
Collecting async-timeout>=4.0.3 (from asyncpg==0.29.0->-r requirements.txt (line 8))
  Using cached async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)
Collecting PyMuPDFb==1.24.3 (from PyMuPDF==1.24.3->-r requirements.txt (line 11))
  Using cached PyMuPDFb-1.24.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (1.4 kB)
Collecting et-xmlfile (from openpyxl==3.1.2->-r requirements.txt (line 12))
  Using cached et_xmlfile-2.0.0-py3-none-any.whl.metadata (2.7 kB)
Collecting lxml>=3.1.0 (from python-docx==1.1.2->-r requirements.txt (line 13))
  Using cached lxml-6.1.1-cp311-cp311-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl.metadata (3.5 kB)
Collecting anyio<5,>=3.5.0 (from openai==1.30.0->-r requirements.txt (line 17))
  Using cached anyio-4.13.0-py3-none-any.whl.metadata (4.5 kB)
Collecting distro<2,>=1.7.0 (from openai==1.30.0->-r requirements.txt (line 17))
  Using cached distro-1.9.0-py3-none-any.whl.metadata (6.8 kB)
Collecting sniffio (from openai==1.30.0->-r requirements.txt (line 17))
  Using cached sniffio-1.3.1-py3-none-any.whl.metadata (3.9 kB)
Collecting tqdm>4 (from openai==1.30.0->-r requirements.txt (line 17))
  Using cached tqdm-4.67.3-py3-none-any.whl.metadata (57 kB)
Collecting jiter<1,>=0.4.0 (from anthropic==0.57.1->-r requirements.txt (line 20))
  Using cached jiter-0.15.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (5.2 kB)
Collecting certifi (from httpx==0.27.0->-r requirements.txt (line 23))
  Using cached certifi-2026.5.20-py3-none-any.whl.metadata (2.5 kB)
Collecting httpcore==1.* (from httpx==0.27.0->-r requirements.txt (line 23))
  Using cached httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
Collecting idna (from httpx==0.27.0->-r requirements.txt (line 23))
  Using cached idna-3.15-py3-none-any.whl.metadata (7.7 kB)
Collecting annotated-types>=0.4.0 (from pydantic==2.7.2->-r requirements.txt (line 25))
  Using cached annotated_types-0.7.0-py3-none-any.whl.metadata (15 kB)
Collecting pydantic-core==2.18.3 (from pydantic==2.7.2->-r requirements.txt (line 25))
  Using cached pydantic_core-2.18.3-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (6.5 kB)
Collecting httptools>=0.5.0 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached httptools-0.7.1-cp311-cp311-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl.metadata (3.5 kB)
Collecting pyyaml>=5.1 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting uvloop!=0.15.0,!=0.15.1,>=0.14.0 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached uvloop-0.22.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (4.9 kB)
Collecting watchfiles>=0.13 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached watchfiles-1.2.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (4.9 kB)
Collecting websockets>=10.4 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached websockets-16.0-cp311-cp311-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl.metadata (6.8 kB)
Collecting dnspython>=2.0.0 (from email_validator>=2.0.0->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached dnspython-2.8.0-py3-none-any.whl.metadata (5.7 kB)
Collecting typer>=0.16.0 (from fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached typer-0.25.1-py3-none-any.whl.metadata (15 kB)
Collecting rich-toolkit>=0.14.8 (from fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached rich_toolkit-0.19.9-py3-none-any.whl.metadata (1.0 kB)
Collecting pyjwt<3.0.0,>=2.10.1 (from gotrue<3.0,>=1.3->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached pyjwt-2.12.1-py3-none-any.whl.metadata (4.1 kB)
Collecting MarkupSafe>=2.0 (from jinja2>=2.11.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached markupsafe-3.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.7 kB)
Collecting deprecation<3.0.0,>=2.1.0 (from postgrest<0.17.0,>=0.14->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached deprecation-2.1.0-py2.py3-none-any.whl.metadata (4.6 kB)
Collecting strenum<0.5.0,>=0.4.9 (from postgrest<0.17.0,>=0.14->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached StrEnum-0.4.15-py3-none-any.whl.metadata (5.3 kB)
Collecting python-dateutil<3.0.0,>=2.8.1 (from realtime<2.0.0,>=1.0.0->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached python_dateutil-2.9.0.post0-py2.py3-none-any.whl.metadata (8.4 kB)
Collecting websockets>=10.4 (from uvicorn[standard]==0.29.0->-r requirements.txt (line 3))
  Using cached websockets-12.0-cp311-cp311-manylinux_2_5_x86_64.manylinux1_x86_64.manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (6.6 kB)
Collecting packaging (from deprecation<3.0.0,>=2.1.0->postgrest<0.17.0,>=0.14->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached packaging-26.2-py3-none-any.whl.metadata (3.5 kB)
Collecting h2<5,>=3 (from httpx[http2]<0.29,>=0.26->gotrue<3.0,>=1.3->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached h2-4.3.0-py3-none-any.whl.metadata (5.1 kB)
Collecting six>=1.5 (from python-dateutil<3.0.0,>=2.8.1->realtime<2.0.0,>=1.0.0->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached six-1.17.0-py2.py3-none-any.whl.metadata (1.7 kB)
Collecting rich>=13.7.1 (from rich-toolkit>=0.14.8->fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached rich-15.0.0-py3-none-any.whl.metadata (18 kB)
Collecting shellingham>=1.3.0 (from typer>=0.16.0->fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached shellingham-1.5.4-py2.py3-none-any.whl.metadata (3.5 kB)
Collecting annotated-doc>=0.0.2 (from typer>=0.16.0->fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached annotated_doc-0.0.4-py3-none-any.whl.metadata (6.6 kB)
Collecting hyperframe<7,>=6.1 (from h2<5,>=3->httpx[http2]<0.29,>=0.26->gotrue<3.0,>=1.3->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached hyperframe-6.1.0-py3-none-any.whl.metadata (4.3 kB)
Collecting hpack<5,>=4.1 (from h2<5,>=3->httpx[http2]<0.29,>=0.26->gotrue<3.0,>=1.3->supabase==2.5.0->-r requirements.txt (line 7))
  Using cached hpack-4.1.0-py3-none-any.whl.metadata (4.6 kB)
Collecting markdown-it-py>=2.2.0 (from rich>=13.7.1->rich-toolkit>=0.14.8->fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached markdown_it_py-4.2.0-py3-none-any.whl.metadata (7.4 kB)
Collecting pygments<3.0.0,>=2.13.0 (from rich>=13.7.1->rich-toolkit>=0.14.8->fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached pygments-2.20.0-py3-none-any.whl.metadata (2.5 kB)
Collecting mdurl~=0.1 (from markdown-it-py>=2.2.0->rich>=13.7.1->rich-toolkit>=0.14.8->fastapi-cli>=0.0.2->fastapi==0.111.0->-r requirements.txt (line 2))
  Using cached mdurl-0.1.2-py3-none-any.whl.metadata (1.6 kB)
Using cached fastapi-0.111.0-py3-none-any.whl (91 kB)
Using cached uvicorn-0.29.0-py3-none-any.whl (60 kB)
Using cached python_multipart-0.0.9-py3-none-any.whl (22 kB)
Using cached supabase-2.5.0-py3-none-any.whl (16 kB)
Using cached asyncpg-0.29.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (2.8 MB)
Using cached PyMuPDF-1.24.3-cp311-none-manylinux2014_x86_64.whl (3.5 MB)
Using cached openpyxl-3.1.2-py2.py3-none-any.whl (249 kB)
Using cached python_docx-1.1.2-py3-none-any.whl (244 kB)
Using cached duckdb-0.10.3-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (18.5 MB)
Using cached openai-1.30.0-py3-none-any.whl (320 kB)
Using cached anthropic-0.57.1-py3-none-any.whl (292 kB)
Using cached httpx-0.27.0-py3-none-any.whl (75 kB)
Using cached python_dotenv-1.0.1-py3-none-any.whl (19 kB)
Using cached pydantic-2.7.2-py3-none-any.whl (409 kB)
Using cached pydantic_settings-2.3.0-py3-none-any.whl (21 kB)
Using cached httpcore-1.0.9-py3-none-any.whl (78 kB)
Using cached pydantic_core-2.18.3-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (2.0 MB)
Using cached PyMuPDFb-1.24.3-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (15.8 MB)
Using cached annotated_types-0.7.0-py3-none-any.whl (13 kB)
Using cached anyio-4.13.0-py3-none-any.whl (114 kB)
Using cached async_timeout-5.0.1-py3-none-any.whl (6.2 kB)
Using cached click-8.4.0-py3-none-any.whl (116 kB)
Using cached distro-1.9.0-py3-none-any.whl (20 kB)
Using cached email_validator-2.3.0-py3-none-any.whl (35 kB)
Using cached fastapi_cli-0.0.24-py3-none-any.whl (12 kB)
Using cached gotrue-2.12.4-py3-none-any.whl (42 kB)
Using cached h11-0.16.0-py3-none-any.whl (37 kB)
Using cached httptools-0.7.1-cp311-cp311-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl (456 kB)
Using cached idna-3.15-py3-none-any.whl (72 kB)
Using cached jinja2-3.1.6-py3-none-any.whl (134 kB)
Using cached jiter-0.15.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (349 kB)
Using cached lxml-6.1.1-cp311-cp311-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl (5.2 MB)
Using cached orjson-3.11.9-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (133 kB)
Using cached postgrest-0.16.11-py3-none-any.whl (21 kB)
Using cached pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (806 kB)
Using cached realtime-1.0.6-py3-none-any.whl (9.0 kB)
Using cached starlette-0.37.2-py3-none-any.whl (71 kB)
Using cached storage3-0.7.7-py3-none-any.whl (16 kB)
Using cached supafunc-0.4.7-py3-none-any.whl (6.2 kB)
Using cached tqdm-4.67.3-py3-none-any.whl (78 kB)
Using cached typing_extensions-4.15.0-py3-none-any.whl (44 kB)
Using cached ujson-5.12.1-cp311-cp311-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl (59 kB)
Using cached uvloop-0.22.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (3.8 MB)
Using cached watchfiles-1.2.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (456 kB)
Using cached websockets-12.0-cp311-cp311-manylinux_2_5_x86_64.manylinux1_x86_64.manylinux_2_17_x86_64.manylinux2014_x86_64.whl (130 kB)
Using cached certifi-2026.5.20-py3-none-any.whl (134 kB)
Using cached et_xmlfile-2.0.0-py3-none-any.whl (18 kB)
Using cached sniffio-1.3.1-py3-none-any.whl (10 kB)
Using cached deprecation-2.1.0-py2.py3-none-any.whl (11 kB)
Using cached dnspython-2.8.0-py3-none-any.whl (331 kB)
Using cached markupsafe-3.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (22 kB)
Using cached pyjwt-2.12.1-py3-none-any.whl (29 kB)
Using cached python_dateutil-2.9.0.post0-py2.py3-none-any.whl (229 kB)
Using cached rich_toolkit-0.19.9-py3-none-any.whl (33 kB)
Using cached StrEnum-0.4.15-py3-none-any.whl (8.9 kB)
Using cached typer-0.25.1-py3-none-any.whl (58 kB)
Using cached annotated_doc-0.0.4-py3-none-any.whl (5.3 kB)
Using cached h2-4.3.0-py3-none-any.whl (61 kB)
Using cached rich-15.0.0-py3-none-any.whl (310 kB)
Using cached shellingham-1.5.4-py2.py3-none-any.whl (9.8 kB)
Using cached six-1.17.0-py2.py3-none-any.whl (11 kB)
Using cached packaging-26.2-py3-none-any.whl (100 kB)
Using cached hpack-4.1.0-py3-none-any.whl (34 kB)
Using cached hyperframe-6.1.0-py3-none-any.whl (13 kB)
Using cached markdown_it_py-4.2.0-py3-none-any.whl (91 kB)
Using cached pygments-2.20.0-py3-none-any.whl (1.2 MB)
Using cached mdurl-0.1.2-py3-none-any.whl (10.0 kB)
Installing collected packages: strenum, websockets, uvloop, ujson, typing-extensions, tqdm, sniffio, six, shellingham, pyyaml, python-multipart, python-dotenv, PyMuPDFb, pyjwt, pygments, packaging, orjson, mdurl, MarkupSafe, lxml, jiter, idna, hyperframe, httptools, hpack, h11, et-xmlfile, duckdb, dnspython, distro, click, certifi, async-timeout, annotated-types, annotated-doc, uvicorn, python-docx, python-dateutil, PyMuPDF, pydantic-core, openpyxl, markdown-it-py, jinja2, httpcore, h2, email_validator, deprecation, asyncpg, anyio, watchfiles, starlette, rich, realtime, pydantic, httpx, typer, rich-toolkit, pydantic-settings, openai, anthropic, supafunc, storage3, postgrest, gotrue, fastapi-cli, supabase, fastapi
Successfully installed MarkupSafe-3.0.3 PyMuPDF-1.24.3 PyMuPDFb-1.24.3 annotated-doc-0.0.4 annotated-types-0.7.0 anthropic-0.57.1 anyio-4.13.0 async-timeout-5.0.1 asyncpg-0.29.0 certifi-2026.5.20 click-8.4.0 deprecation-2.1.0 distro-1.9.0 dnspython-2.8.0 duckdb-0.10.3 email_validator-2.3.0 et-xmlfile-2.0.0 fastapi-0.111.0 fastapi-cli-0.0.24 gotrue-2.12.4 h11-0.16.0 h2-4.3.0 hpack-4.1.0 httpcore-1.0.9 httptools-0.7.1 httpx-0.27.0 hyperframe-6.1.0 idna-3.15 jinja2-3.1.6 jiter-0.15.0 lxml-6.1.1 markdown-it-py-4.2.0 mdurl-0.1.2 openai-1.30.0 openpyxl-3.1.2 orjson-3.11.9 packaging-26.2 postgrest-0.16.11 pydantic-2.7.2 pydantic-core-2.18.3 pydantic-settings-2.3.0 pygments-2.20.0 pyjwt-2.12.1 python-dateutil-2.9.0.post0 python-docx-1.1.2 python-dotenv-1.0.1 python-multipart-0.0.9 pyyaml-6.0.3 realtime-1.0.6 rich-15.0.0 rich-toolkit-0.19.9 shellingham-1.5.4 six-1.17.0 sniffio-1.3.1 starlette-0.37.2 storage3-0.7.7 strenum-0.4.15 supabase-2.5.0 supafunc-0.4.7 tqdm-4.67.3 typer-0.25.1 typing-extensions-4.15.0 ujson-5.12.1 uvicorn-0.29.0 uvloop-0.22.1 watchfiles-1.2.0 websockets-12.0
[notice] A new release of pip is available: 24.0 -> 26.1.1
[notice] To update, run: pip install --upgrade pip
==> Uploading build...
==> Uploaded in 3.3s. Compression took 2.3s
==> Build successful 🎉
==> Deploying...
==> Setting WEB_CONCURRENCY=1 by default, based on available CPUs in the instance
==> Running 'uvicorn main:app --host 0.0.0.0 --port $PORT'
Traceback (most recent call last):
  File "/opt/render/project/src/.venv/bin/uvicorn", line 8, in <module>
    sys.exit(main())
             ^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/click/core.py", line 1524, in __call__
    return self.main(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/click/core.py", line 1445, in main
    rv = self.invoke(ctx)
         ^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/click/core.py", line 1308, in invoke
    return ctx.invoke(self.callback, **ctx.params)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/click/core.py", line 877, in invoke
    return callback(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/main.py", line 409, in main
    run(
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/main.py", line 575, in run
    server.run()
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/server.py", line 65, in run
    return asyncio.run(self.serve(sockets=sockets))
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/python/Python-3.11.11/lib/python3.11/asyncio/runners.py", line 190, in run
    return runner.run(main)
           ^^^^^^^^^^^^^^^^
  File "/opt/render/project/python/Python-3.11.11/lib/python3.11/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "uvloop/loop.pyx", line 1518, in uvloop.loop.Loop.run_until_complete
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/server.py", line 69, in serve
    await self._serve(sockets)
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/server.py", line 76, in _serve
    config.load()
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/config.py", line 433, in load
    self.loaded_app = import_from_string(self.app)
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/uvicorn/importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/python/Python-3.11.11/lib/python3.11/importlib/__init__.py", line 126, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1204, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1176, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1147, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 690, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 936, in exec_module
  File "<frozen importlib._bootstrap_external>", line 1074, in get_code
  File "<frozen importlib._bootstrap_external>", line 1004, in source_to_code
  File "<frozen importlib._bootstrap>", line 241, in _call_with_frames_removed
  File "/opt/render/project/src/backend/main.py", line 629
    async with ant.messages.stream(
                                  ^
SyntaxError: '(' was never closed
==> Exited with status 1
==> Common ways to troubleshoot your deploy: https://render.com/docs/troubleshooting-deploys
==> Running 'uvicorn main:app --host 0.0.0.0 --port $PORT'