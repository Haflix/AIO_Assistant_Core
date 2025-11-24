# AI Plugin

This file contains the plans for the AI integration of the plugin system.

## Requirements

These are the general requirements that the final plugin should meet:

- Tool-usage: For RAG and actions
- Load the model
- Handle incoming queries one by one
- Talking modes. For example: Talkative or mostly just executing the command
- Not horrible response time

### Tools

In this section, I will list tools that could help me to minimize hallucinations and overall performance:

- GBNF: Model will only be able to form correct commands and cannot hallucinate non-existing endpoints
- Semantic search: Used to find commands based on input. Would yield the top results.
- Last resorts: LoRa and fine-tuning

## The pipeline

This part has many approaches or ways of doing it. Being able to change between these modes may be a good idea.
