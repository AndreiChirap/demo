import os
import dotenv
from time import time
import streamlit as st

from langchain_community.document_loaders.text import TextLoader
from langchain_community.document_loaders import (
    WebBaseLoader,
    PyPDFLoader,
    Docx2txtLoader,
)
# pip install docx2txt, pypdf
from langchain_community.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, AzureOpenAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain

dotenv.load_dotenv()

os.environ["USER_AGENT"] = "myagent"
DB_DOCS_LIMIT = 1000

# Function to stream the response of the LLM
def stream_llm_response(llm_stream, messages):
    response_message = ""

    for chunk in llm_stream.stream(messages):
        response_message += chunk.content
        yield chunk

    st.session_state.messages.append({"role": "assistant", "content": response_message})


# --- Indexing Phase ---

def load_doc_to_db():
    # Use loader according to doc type
    if "rag_docs" in st.session_state and st.session_state.rag_docs:
        docs = []
        for doc_file in st.session_state.rag_docs:
            if doc_file.name not in st.session_state.rag_sources:
                if len(st.session_state.rag_sources) < DB_DOCS_LIMIT:
                    os.makedirs("source_files", exist_ok=True)
                    file_path = f"./source_files/{doc_file.name}"
                    with open(file_path, "wb") as file:
                        file.write(doc_file.read())

                    try:
                        if doc_file.type == "application/pdf":
                            loader = PyPDFLoader(file_path)
                        elif doc_file.name.endswith(".docx"):
                            loader = Docx2txtLoader(file_path)
                        elif doc_file.type in ["text/plain", "text/markdown"]:
                            loader = TextLoader(file_path)
                        else:
                            st.warning(f"Document type {doc_file.type} not supported.")
                            continue

                        docs.extend(loader.load())
                        st.session_state.rag_sources.append(doc_file.name)

                    except Exception as e:
                        st.toast(f"Error loading document {doc_file.name}: {e}", icon="⚠️")
                        print(f"Error loading document {doc_file.name}: {e}")
                    finally:
                        os.remove(file_path)

                else:
                    st.error(F"Maximum number of documents reached ({DB_DOCS_LIMIT}).")

        if docs:
            _split_and_load_docs(docs)
            st.toast(f"Document *{str([doc_file.name for doc_file in st.session_state.rag_docs])[1:-1]}* loaded successfully.", icon="✅")


def load_url_to_db():
    if "rag_url" in st.session_state and st.session_state.rag_url:
        url = st.session_state.rag_url
        docs = []
        if url not in st.session_state.rag_sources:
            if len(st.session_state.rag_sources) < 10:
                try:
                    loader = WebBaseLoader(url)
                    docs.extend(loader.load())
                    st.session_state.rag_sources.append(url)

                except Exception as e:
                    st.error(f"Error loading document from {url}: {e}")

                if docs:
                    _split_and_load_docs(docs)
                    st.toast(f"Document from URL *{url}* loaded successfully.", icon="✅")

            else:
                st.error("Maximum number of documents reached (10).")


def initialize_vector_db(docs):
    if "AZ_OPENAI_API_KEY" not in os.environ:
        embedding = OpenAIEmbeddings(api_key=st.session_state.openai_api_key)
    else:
        embedding = AzureOpenAIEmbeddings(
            api_key=os.getenv("AZ_OPENAI_API_KEY"),
            azure_endpoint=os.getenv("AZ_OPENAI_ENDPOINT"),
            model="text-embedding-3-large",
            openai_api_version="2024-02-15-preview",
        )

    vector_db = Chroma.from_documents(
        documents=docs,
        embedding=embedding,
        collection_name=f"{str(time()).replace('.', '')[:14]}_" + st.session_state['session_id'],
    )

    # We need to manage the number of collections that we have in memory, we will keep the last 20
    chroma_client = vector_db._client
    collection_names = sorted([collection.name for collection in chroma_client.list_collections()])
    print("Number of collections:", len(collection_names))
    while len(collection_names) > 20:
        chroma_client.delete_collection(collection_names[0])
        collection_names.pop(0)

    return vector_db


def _split_and_load_docs(docs):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=5000,
        chunk_overlap=1000,
    )

    document_chunks = text_splitter.split_documents(docs)

    if "vector_db" not in st.session_state:
        st.session_state.vector_db = initialize_vector_db(docs)
    else:
        st.session_state.vector_db.add_documents(document_chunks)


# --- Retrieval Augmented Generation (RAG) Phase ---

def _get_context_retriever_chain(vector_db, llm):
    retriever = vector_db.as_retriever()
    prompt = ChatPromptTemplate.from_messages([
        MessagesPlaceholder(variable_name="messages"),
        ("user", "{input}"),
        ("user", "Given the above conversation, generate a search query to look up in order to get inforamtion relevant to the conversation"),
    ])
    retriever_chain = create_history_aware_retriever(llm, retriever, prompt)

    return retriever_chain


def get_conversational_rag_chain(llm):
    retriever_chain = _get_context_retriever_chain(st.session_state.vector_db, llm)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
        """You are a specialized AI assistant for Property Managers, focused on analyzing and categorizing resident support tickets. Your primary function is to match tickets with the most appropriate service based on the provided service description.

Core Responsibilities:
1. Analyze incoming resident tickets
2. Match the ticket content with relevant service categories

Matching Rules:
- If multiple services are relevant, return the top 3 most appropriate matches, ranked by relevance
- If no service matches the ticket, explicitly state "No matching service found"
- Always provide a brief explanation for why each recommended service is relevant

Response Format:
For matching tickets:
- Primary Match: [SERVICE_NAME] - [Brief justification]
- Additional Matches (if any):
  1. [SERVICE_NAME] - [Brief justification]
  2. [SERVICE_NAME] - [Brief justification]

For non-matching tickets:
- Result: No matching Service found
- Reason: [Brief explanation why no SKUs match]

Important:
- Only recommend SERVICES that are explicitly relevant to the ticket
- Do not attempt to force matches when none exist
- Consider both explicit and implicit technical requirements in the ticket:
-----------------------------------------------------------------------------------------------------------------

{context}
"""),
        MessagesPlaceholder(variable_name="messages"),
        ("user", "{input}"),
    ])
    stuff_documents_chain = create_stuff_documents_chain(llm, prompt)

    return create_retrieval_chain(retriever_chain, stuff_documents_chain)


def stream_llm_rag_response(llm_stream, messages):
    conversation_rag_chain = get_conversational_rag_chain(llm_stream)
    response_message = "*(RAG Response)*\n"
    for chunk in conversation_rag_chain.pick("answer").stream({"messages": messages[:-1], "input": messages[-1].content}):
        response_message += chunk
        yield chunk

    st.session_state.messages.append({"role": "assistant", "content": response_message})
